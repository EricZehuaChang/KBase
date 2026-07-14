"""批量导入（企业级体量：万~十万份文件）。

网页上传适合日常增量，不适合首次灌库——浏览器/HTTP 超时、无断点续传、
BackgroundTasks 进程内任务重启即丢。本工具直接驱动 IngestPipeline（绕过
web 层），面向"服务器上一个目录里放着全部历史文件"的真实交付场景：

- **清单驱动断点续传**：每处理一个文件就追加一行 manifest（JSONL），
  中断后重跑自动跳过已完成项（按 路径+大小+mtime 匹配，文件变了会重导）；
- **失败重跑**：--retry-failed 只重跑上次失败清单；
- **并发可调**：--workers 控制线程数（嵌入走 GPU/TEI 时可开大；
  云 API 受各家 QPS 限制，建议 2~4）；
- 去重靠管道内置 content_hash（同库同内容自动跳过，不重复向量化）。

用法：
    python -m kbase.bulk_import --kb <kb_id> --dir <目录> \\
        [--config config/kbase.yaml] [--workers 4] [--parse-mode auto|ocr] \\
        [--manifest data/import-manifest.jsonl] [--retry-failed]
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock

# 与 web 上传口径一致的可导入类型（vlm 模式需人工校验，不适合批量，故不提供）
SUPPORTED_EXTS = {".md", ".txt", ".docx", ".xlsx", ".pptx", ".pdf",
                  ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".html"}


def scan_files(root: Path) -> list[Path]:
    """递归收集支持类型的文件，稳定排序（清单可比对、重跑顺序一致）。"""
    return sorted(p for p in Path(root).rglob("*")
                  if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS)


def _entry_key(path: Path) -> dict:
    st = path.stat()
    return {"path": str(path), "size": st.st_size, "mtime": int(st.st_mtime)}


def load_manifest(manifest_path: Path) -> dict[str, dict]:
    """读清单（JSONL，后写覆盖先写——重跑后以最新状态为准）。
    键 = path|size|mtime，文件内容变化会得到新键从而重新导入。"""
    entries: dict[str, dict] = {}
    if not manifest_path.exists():
        return entries
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            entries[f"{e['path']}|{e['size']}|{e['mtime']}"] = e
        except (json.JSONDecodeError, KeyError):
            continue                     # 坏行跳过，不让一行损坏毁掉续传
    return entries


def plan_pending(files: list[Path], manifest: dict[str, dict],
                 retry_failed: bool = False) -> list[Path]:
    """决定本轮要处理哪些文件。
    默认模式：done 跳过（断点续传核心）；**failed 自动带上重试**；
    新文件/内容已变（size 或 mtime 变→清单键不同）纳入。
    retry_failed=True：只跑上次 failed 项，不扫新文件（定向修复模式）。"""
    pending = []
    for p in files:
        key = "{path}|{size}|{mtime}".format(**_entry_key(p))
        prev = manifest.get(key)
        if prev is None:
            if not retry_failed:
                pending.append(p)
        elif prev.get("status") == "failed":
            pending.append(p)
        # done：跳过（断点续传的核心）
    return pending


def run_import(pipeline, sf, kb_id: str, files: list[Path],
               manifest_path: Path, workers: int = 4,
               parse_mode: str = "auto", log=print) -> dict:
    """执行导入并逐文件落清单。返回汇总 {done, failed, skipped_dup}。
    pipeline.ingest_file 内部已保证单文件失败不抛（批次隔离），这里额外
    读回文档状态判定 failed（如解析失败/OCR 缺配置），写进清单供重跑。"""
    from kbase.models import Document

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lock = Lock()
    stats = {"done": 0, "failed": 0}
    started = time.time()

    def _record(entry: dict) -> None:
        with lock:
            with manifest_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _one(path: Path) -> None:
        entry = {**_entry_key(path), "ts": datetime.utcnow().isoformat()}
        try:
            doc_id = pipeline.ingest_file(kb_id, path, path.name,
                                          parse_mode=parse_mode)
            with sf() as s:
                doc = s.get(Document, doc_id)
                status, error = (doc.status, doc.error) if doc else ("failed", "文档行缺失")
            if status in ("ready", "pending_ocr", "pending_review"):
                # pending_ocr/review 属"已受理待后续动作"，不算失败，
                # 由页面批量重试/校验收尾
                entry.update(status="done", doc_id=doc_id, doc_status=status)
                stats["done"] += 1
            else:
                entry.update(status="failed", doc_id=doc_id, error=error)
                stats["failed"] += 1
        except Exception as e:  # noqa: BLE001 —— 单文件任何异常不毁批次
            entry.update(status="failed", error=f"{type(e).__name__}: {e}")
            stats["failed"] += 1
        _record(entry)

    total = len(files)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(_one, p) for p in files]
        for i, fut in enumerate(as_completed(futures), 1):
            fut.result()
            if i % 50 == 0 or i == total:
                elapsed = time.time() - started
                rate = i / elapsed if elapsed else 0
                eta = (total - i) / rate if rate else 0
                log(f"[{i}/{total}] 速率 {rate:.1f} 文件/秒，"
                    f"预计剩余 {eta/60:.1f} 分钟，失败 {stats['failed']}")
    stats["elapsed_s"] = round(time.time() - started, 1)
    return stats


def _main() -> None:
    parser = argparse.ArgumentParser(description="批量导入目录到指定知识库（断点续传）")
    parser.add_argument("--kb", required=True, help="知识库 id")
    parser.add_argument("--dir", required=True, help="要导入的根目录（递归）")
    parser.add_argument("--config", default="config/kbase.yaml")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--parse-mode", default="auto", choices=["auto", "ocr"])
    parser.add_argument("--manifest", default=None,
                        help="清单文件路径（默认 data_dir/import-<kb>.jsonl）")
    parser.add_argument("--retry-failed", action="store_true",
                        help="只重跑清单里的失败项")
    args = parser.parse_args()

    # 复用生产装配（含 KB 级向量模型/OCR/VLM 配置），与 web 摄取语义完全一致
    from kbase.api.services import build_services
    svc = build_services(args.config)
    manifest_path = (Path(args.manifest) if args.manifest
                     else svc.cfg.data_dir / f"import-{args.kb}.jsonl")

    files = scan_files(Path(args.dir))
    manifest = load_manifest(manifest_path)
    pending = plan_pending(files, manifest, retry_failed=args.retry_failed)
    print(f"发现 {len(files)} 个文件，本轮待处理 {len(pending)} 个"
          f"（清单: {manifest_path}）")
    if not pending:
        print("没有需要处理的文件（全部已完成，或用 --retry-failed 重跑失败项）")
        return
    stats = run_import(svc.pipeline, svc.sf, args.kb, pending, manifest_path,
                       workers=args.workers, parse_mode=args.parse_mode)
    print(f"完成：成功 {stats['done']}，失败 {stats['failed']}，"
          f"耗时 {stats['elapsed_s']}s；失败项可用 --retry-failed 重跑")


if __name__ == "__main__":
    _main()
