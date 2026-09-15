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

T18：每轮运行在 import_batches 表里落一行（起止时间/发起人/计数/终态），
只读接口（kbase/api/routes/import_batches.py）读它。**本文件是唯一的写侧，
并且只从命令行进入**——HTTP 侧刻意没有任何触发导入的端点。

用法：
    python -m kbase.bulk_import --kb <kb_id> --dir <目录> \\
        [--config config/kbase.yaml] [--workers 4] [--parse-mode auto|ocr] \\
        [--manifest data/import-manifest.jsonl] [--retry-failed]
"""
import argparse
import getpass
import json
import os
import re
import signal
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread

from kbase.models import ImportBatch, KnowledgeBase

# 批次状态机（与 models.ImportBatch 的注释一致，取值写死在这里而不是自由文本）
ST_RUNNING = "running"                    # 运行中（finished_at 为 NULL）
ST_DONE = "done"                          # 全部成功
ST_DONE_WITH_ERRORS = "done_with_errors"  # 跑完了但有失败文件（可 --retry-failed）
ST_FAILED = "failed"                      # 一个都没成功（多为配置/环境问题）
ST_INTERRUPTED = "interrupted"            # 被中断/崩溃，没跑完（可续传）

# 停止信号：SIGINT=Ctrl-C，SIGTERM=kill/容器停，SIGHUP=终端断开（nohup 长跑
# 时会遇到）。都转成异常，让收尾逻辑走 finally 把批次写成 interrupted。
_STOP_SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP")

# 清单条目 CSV 导出的列及顺序：路由层按它拼表头，测试按它断言列齐——
# 只在这里定义一次（与 qa_outcomes.EXPORT_COLUMNS 同一手法）。
IMPORT_COLUMNS = ("path", "status", "size", "mtime", "doc_id", "doc_status",
                  "error", "ts")

# 导出上限：与 qa_outcomes.EXPORT_MAX_ROWS 同一考虑——导出是人工排障产物，
# 响应体与内存都要有边界；要看全量明细直接读清单文件（它就是逐文件台账）。
EXPORT_MAX_ROWS = 5000

# 心跳间隔与"判死"阈值。阈值取 3 倍间隔：心跳线程被 GIL/长任务挤住一两拍是
# 正常的，比它更久没声才当死。两个值都只在 CLI 运行期有意义。
HEARTBEAT_SECONDS = 60
STALE_AFTER_SECONDS = 180

# 批次 id 的严格形态：uuid4 的 str()。任何请求参数在选择批次行之前先过这一
# 关——它同时挡掉「../」这类穿越意图（不是 uuid 形态即拒），别在别处放宽。
_BATCH_ID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

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
               parse_mode: str = "auto", log=print,
               batch_id: str | None = None, manages_batch: bool = False) -> dict:
    """执行导入并逐文件落清单。返回汇总 {done, failed, skipped_dup}。
    pipeline.ingest_file 内部已保证单文件失败不抛（批次隔离），这里额外
    读回文档状态判定 failed（如解析失败/OCR 缺配置），写进清单供重跑。

    batch_id 非空时把本轮汇总写回该批次行（T18）。manages_batch=True（CLI
    用法）时本函数用 try/finally 保证**任何退出路径都收尾**：正常完成、单文件
    异常、以及被 _install_stop_handlers 转成异常的 Ctrl-C/kill/SIGHUP。少了
    这个 finally，批次就会永久停在 running——那正是"看着在跑其实早就死了"
    的支持负担。"""
    from kbase.models import Document

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lock = Lock()
    stats = {"done": 0, "failed": 0}
    started = time.time()
    # 收尾状态由 finally 决定；holder 让"被中断"这件事从信号处理函数传出来
    # （信号处理函数不能抛异常到这里，只能记状态）。
    outcome = {"interrupted": False, "error": None, "exit_code": 0}
    context = {"workers": workers, "parse_mode": parse_mode}
    previous = _install_stop_handlers(outcome) if manages_batch else {}
    stop_heartbeat, heartbeat_thread = (None, None)
    if batch_id and manages_batch:
        # 心跳让读接口能把"正在跑的批次"与"进程没了的死行"分开（见
        # reconcile_stale_running）——没有心跳就只能拿起始时间猜，会把长跑
        # 的正常批次误判成崩溃。
        stop_heartbeat, heartbeat_thread = _heartbeat(sf, batch_id)
    finished = False

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
    try:
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
        finished = True
    except BaseException as e:  # noqa: BLE001 —— 含信号转成的 KeyboardInterrupt（不是 Exception）
        outcome["interrupted"] = True
        outcome["error"] = outcome["error"] or f"{type(e).__name__}: {e}"
        raise
    finally:
        if stop_heartbeat:
            stop_heartbeat()             # 先停心跳再收尾：终态行不该再被跳
        if manages_batch:
            _restore_stop_handlers(previous)
        stats["elapsed_s"] = round(time.time() - started, 1)
        if batch_id:
            # 唯一的收尾点：正常跑完与被中断都走这里，批次绝不会停在 running。
            # "跑完但全失败"记 failed 而不是 done_with_errors（0 成功通常意味
            # 着配置/环境问题，不是个别坏文件——两者的排查动作不同）。
            finish_batch(
                sf, batch_id, stats,
                status=_import_status(stats, total) if finished else ST_INTERRUPTED,
                interrupted=outcome["interrupted"], error=outcome["error"],
                context=context, total=total, exit_code=outcome["exit_code"])
    return stats


# ---------------------------------------------------------------------------
# T18 批次账本：CLI 写、HTTP 只读
# ---------------------------------------------------------------------------
# 为什么要有（写在这段代码旁边而不是只写在表注释里）：manifest 是服务器本地
# 文件，交付后没人能回答"昨天那轮灌库跑了没、成功多少"。把批次落库之后，
# 管理端详情页能直接读，并且**中断也能被看见**（而不是无声无息地少跑一半）。


def safe_batch_id(batch_id: str) -> str:
    """批次 id 的显式防御：**先验形态，再查库**。

    任何"选择某个批次"的请求参数都要过这里。id 是 uuid4 的 str()，只允许
    这 36 个字符的安全形态：`../`、`..%2f`、绝对路径、NUL 一律落不进来
    （`os`/`Path` 层面压根拿不到这个值），因此不存在"用 batch_id 拼出文件
    路径"的通道。非法形态不抛异常而是返回空串，由调用方当"批次不存在"处理
    ——不泄漏"这个 id 形态对不对"。
    """
    return batch_id if isinstance(batch_id, str) and _BATCH_ID_RE.match(batch_id) else ""


def _within(candidate: Path, root: Path) -> bool:
    """candidate 是否落在 root 内。

    先 resolve 再比——`..` 与符号链接都被展开，`data_dir/../../etc/passwd`
    解析后不在 root 内，直接判否。不能用字符串前缀比（`/data` 会匹配上
    `/data2`）。"""
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def resolve_manifest_path(data_dir: Path, raw: str) -> Path:
    """清单路径 → 绝对路径，**越界直接拒绝**（T18 路径安全）。

    manifest_path 存在数据库里，随后被读接口打开；一旦能存进任意路径，就等于
    给了一个"用一次 CLI 参数换一次任意文件读"的口子。两道闸门：
    ①拒绝含 `..` 或绝对路径的输入（清单是 data_dir 下的产品文件，不需要
      指向别处）；
    ②resolve 后仍要求落在 data_dir 内（挡符号链接、以及 `..` 混在中间的情况）。
    两道都过才返回；否则抛 ValueError，由调用方转成明确的用户错误——
    这是 CLI/内部调用，静默降级只会让人以为导入在跑。
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("清单路径为空")
    if Path(raw).is_absolute():
        raise ValueError(f"清单路径必须是 data_dir 下的相对路径: {raw}")
    parts = Path(raw).parts
    if ".." in parts:
        raise ValueError(f"清单路径不允许包含 '..': {raw}")
    candidate = data_dir / raw
    if not _within(candidate, data_dir):
        raise ValueError(f"清单路径越界（必须在 {data_dir} 内）: {raw}")
    return candidate


def manifest_available(data_dir: Path, raw: str) -> bool:
    """读侧专用：manifest_path 能否安全打开。**绝不抛**——一个越界的旧行
    应当让该批次"看不到明细"，而不是把列表/导出接口打成 500（写侧已经拒过，
    这是对历史/手工改库数据的兜底）。"""
    try:
        return resolve_manifest_path(data_dir, raw).exists()
    except (ValueError, OSError):
        return False


def _manifest_rel(data_dir: Path, manifest_path: Path) -> str:
    """写侧的清单路径归一：落库一律相对 data_dir。"""
    path = Path(manifest_path)
    if not path.is_absolute():
        return resolve_manifest_path(data_dir, str(path)).relative_to(
            Path(data_dir).resolve()).as_posix()
    resolved = path.resolve()
    if not _within(resolved, data_dir):
        raise ValueError(
            f"清单文件必须放在 data_dir ({data_dir}) 内才能登记批次: {manifest_path}")
    return resolved.relative_to(Path(data_dir).resolve()).as_posix()


def _import_status(stats: dict, total: int | None = None) -> str:
    """一轮导入跑完后的终态（跑完才调用；中断另算）。"""
    done, failed = int(stats.get("done", 0)), int(stats.get("failed", 0))
    if failed == 0 and (done > 0 or not total):
        return ST_DONE
    if done == 0:
        return ST_FAILED
    return ST_DONE_WITH_ERRORS


def _load_summary(raw: str | None) -> dict:
    """counts 列 → dict。NULL/脏数据一律当"还没写汇总"返回 {}，不抛。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _batch_counts(status: str, stats: dict, context: dict, total: int,
                  interrupted: bool = False, error: str | None = None,
                  exit_code: int = 0) -> dict:
    """批次的汇总 JSON（运行结束才完整；明细在清单文件里，本表只存汇总）。

    context 先展开、定值后覆盖：**total/done/failed/status 以本次调用为准**，
    否则起始行留下的 total=0 会把收尾时算出来的真实计数盖掉。
    """
    counts = {k: v for k, v in context.items() if v is not None}
    counts.update({
        "status": status, "total": int(total),
        "pending": max(0, int(total) - int(stats.get("done", 0))
                       - int(stats.get("failed", 0))),
        "done": int(stats.get("done", 0)),
        "failed": int(stats.get("failed", 0)),
        "elapsed_s": stats.get("elapsed_s")})
    if interrupted:
        counts["interrupted"] = True
    if error:
        counts["error"] = str(error)[:500]
    if exit_code:
        counts["exit_code"] = int(exit_code)
    return counts


def start_batch(sf, kb_id: str, manifest_path: Path, data_dir: Path, *,
                started_by: str | None = None, context: dict | None = None,
                dir_path: str | None = None, total: int | None = None) -> str:
    """开一轮导入：插一行 running 批次，返回 batch_id。

    先插行再干活（而不是等跑完再补记）：中途掉电/被 kill 时，库里至少留下
    "有这么一轮、什么时候开始"，后续启动时才能把它回收成 interrupted。
    """
    rel = _manifest_rel(data_dir, manifest_path)
    ctx = dict(context or {})
    if dir_path is not None:
        ctx["dir"] = str(dir_path)
    now = datetime.utcnow()
    row = ImportBatch(
        id=str(uuid.uuid4()), kb_id=kb_id, manifest_path=rel,
        started_by=(started_by or None), started_at=now,
        status=ST_RUNNING, heartbeat_at=now,
        # 起始行也带一份 counts：运行中也能看到"本轮待处理 N 个"，
        # 而不是一个空 JSON。
        counts=json.dumps(_batch_counts(ST_RUNNING, {"done": 0, "failed": 0},
                                        ctx, total or 0),
                          ensure_ascii=False))
    with sf() as s:
        if s.get(KnowledgeBase, kb_id) is None:
            # 库不存在时不至于报外键错（SQLite 默认不校验外键）：显式拦住，
            # 否则批次会指向一个不存在的库，前端列表里就成了孤儿行。
            raise ValueError(f"知识库不存在: {kb_id}")
        s.add(row)
        s.commit()
    return row.id


def _touch_batch(sf, batch_id: str) -> None:
    """写一次心跳。只在批次还停在 running 时写（终态行不再被碰）；任何异常
    一律吞掉——心跳是旁路观测，不能因为它把正在跑的导入打崩。"""
    try:
        with sf() as s:
            (s.query(ImportBatch)
             .filter(ImportBatch.id == batch_id, ImportBatch.status == ST_RUNNING)
             .update({ImportBatch.heartbeat_at: datetime.utcnow()}))
            s.commit()
    except Exception:  # noqa: BLE001 —— 心跳失败不影响导入
        pass


def _heartbeat(sf, batch_id: str, seconds: int = HEARTBEAT_SECONDS):
    """起一个 daemon 心跳线程，返回 (停止函数, 线程)。

    为什么用心跳而不是"拿 started_at 判久"：几万文件的一轮跑几小时很正常，
    只按时间判死会把**正在跑**的批次标成 interrupted（然后把它的收尾写坏）。
    心跳能区分"进程还活着"与"进程没了"——这才是判死需要的信号。

    为什么用线程而不是把心跳塞进主循环：主循环里唯一的周期性位置是
    `i % 50` 那个日志分支，单文件解析几分钟时它不会被执行，心跳就断了。
    """
    stop = Event()

    def _loop() -> None:
        while not stop.wait(seconds):
            _touch_batch(sf, batch_id)

    thread = Thread(target=_loop, daemon=True, name=f"import-heartbeat-{batch_id[:8]}")
    thread.start()
    return stop.set, thread


def finish_batch(sf, batch_id: str, stats: dict, *, status: str,
                 interrupted: bool = False, error: str | None = None,
                 context: dict | None = None, total: int | None = None,
                 exit_code: int = 0) -> bool:
    """收尾：写终态 + 汇总 + finished_at。返回是否命中行。

    幂等且**不覆盖已有终态**：只有停在 running 的行会被写。这样"中断收尾"
    与"一致性回收"谁先跑都不会把对方的结果改成更糟的状态。
    """
    with sf() as s:
        row = s.get(ImportBatch, batch_id)
        if row is None or row.status != ST_RUNNING:
            return False
        prev = _load_summary(row.counts)
        merged = dict(prev)
        merged.update(context or {})
        row.counts = json.dumps(
            _batch_counts(status, stats, merged, total if total is not None
                          else int(prev.get("total", 0)), interrupted=interrupted,
                          error=error, exit_code=exit_code), ensure_ascii=False)
        row.status = status
        row.finished_at = datetime.utcnow()
        row.heartbeat_at = None      # 收尾即停跳，读接口不会再当它活着
        s.commit()
    return True


def reconcile_stale_running(sf, data_dir: Path, *, kb_id: str | None = None,
                            manifest_path: str | None = None,
                            stale_after: int = STALE_AFTER_SECONDS) -> list[str]:
    """回收"停在 running 但进程已经没了"的批次，返回被回收的批次 id 列表。

    中断恢复的两条腿（这是第二条）：
    ①CLI 侧的信号处理 + finally 让 Ctrl-C/kill 能自己收尾；
    ②进程被 SIGKILL/掉电时 finally 不会执行，只剩一条 running 死行——这时候
      由下一次启动（同一 kb+manifest）或读接口的一致性回收来判死：心跳停了
      超过 stale_after 才动它（**正在跑的批次有心跳，不会被误判**）。

    判死之后分两种收尾：清单里的条目已经覆盖全部待处理文件（done+failed
    达到 total）说明那轮其实跑完了、只是崩在写库之前 → done/done_with_errors；
    否则记 interrupted（真被中断在半路），并保留已统计的 done/failed，运维据此
    决定是否重跑（续传本来就是安全的）。

    只挑 running 行：终态行一律不碰（finish_batch 也不覆盖终态，两处一致）。

    data_dir 目前不参与判定（只看心跳与 counts），保留在签名里是为了调用方
    （CLI 与读接口）不必关心判定依据的演进，将来若要按清单反查也无需改调用点。
    """
    reclaimed: list[str] = []
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=max(1, stale_after))
    with sf() as s:
        q = s.query(ImportBatch).filter(ImportBatch.status == ST_RUNNING)
        if kb_id:
            q = q.filter(ImportBatch.kb_id == kb_id)
        if manifest_path:
            q = q.filter(ImportBatch.manifest_path == manifest_path)
        for row in q.all():
            last_seen = row.heartbeat_at or row.started_at
            if last_seen is not None and last_seen > cutoff:
                continue                 # 心跳新鲜：进程大概率还在跑，别动
            prev = _load_summary(row.counts)
            total = int(prev.get("total", 0))
            done = int(prev.get("done", 0))
            failed = int(prev.get("failed", 0))
            finished = total > 0 and done + failed >= total
            status = (ST_INTERRUPTED if not finished
                      else _import_status({"done": done, "failed": failed}, total))
            row.counts = json.dumps(
                _batch_counts(status, {"done": done, "failed": failed,
                                       "elapsed_s": prev.get("elapsed_s")},
                              {k: v for k, v in prev.items()
                               if k in ("workers", "parse_mode", "dir")},
                              total, interrupted=not finished,
                              error="进程未正常结束（心跳超时），已按现场回收"),
                ensure_ascii=False)
            row.status = status
            row.finished_at = now
            reclaimed.append(row.id)
        if reclaimed:
            s.commit()
    return reclaimed


def _batch_row_out(row: ImportBatch) -> dict:
    """批次行的对外投影。summary 是 counts 的同物（读接口名 vs 列名），
    前端只认 summary；把 status 从 JSON 里提到顶层，列表页不必解析 JSON。"""
    summary = _load_summary(row.counts)
    summary["status"] = row.status
    return {"id": row.id, "kb_id": row.kb_id, "manifest_path": row.manifest_path,
            "started_by": row.started_by,
            "started_at": (row.started_at.isoformat() if row.started_at else None),
            "finished_at": (row.finished_at.isoformat()
                            if row.finished_at else None),
            "status": row.status, "summary": summary}


def list_batches(sf, *, kb_id: str | None = None, limit: int = 50) -> dict:
    """批次清单（新→旧），可按库过滤。total 是同条件下的总数。"""
    with sf() as s:
        q = s.query(ImportBatch)
        if kb_id:
            q = q.filter(ImportBatch.kb_id == kb_id)
        total = q.count()
        rows = (q.order_by(ImportBatch.started_at.desc(), ImportBatch.id.desc())
                .limit(limit).all())
        items = [_batch_row_out(r) for r in rows]
    return {"items": items, "total": int(total)}


def get_batch(sf, batch_id: str) -> dict | None:
    """单批次。**先验 id 形态**：非法形态直接当不存在，不做任何路径拼接。"""
    safe = safe_batch_id(batch_id)
    if not safe:
        return None
    with sf() as s:
        row = s.get(ImportBatch, safe)
        return _batch_row_out(row) if row is not None else None


def read_entries(sf, data_dir: Path, batch_id: str, *, failures_only: bool = False,
                 limit: int = 200, offset: int = 0) -> dict:
    """批次明细：读清单 JSONL（后写覆盖先写，同一文件重跑以最新状态为准）。

    明细**不落库**是有意的：万级文件一轮就是万行，塞进批次行的 JSON 会把
    一行撑成几 MB（列表/导出全都要解析它）。清单文件本来就是逐文件台账，
    这里只是把它读出来给页面看。

    清单越界/不存在时不抛：返回空明细 + manifest_available=False，页面显示
    "清单不可读"，而不是整个 tab 500（写侧已拒越界路径，这里是兜底）。
    """
    found = get_batch(sf, batch_id)
    if found is None:
        return {"items": [], "total": 0, "failures": 0,
                "manifest_available": False, "batch": None}
    try:
        manifest_path = resolve_manifest_path(data_dir, found["manifest_path"])
    except ValueError:
        return {"items": [], "total": 0, "failures": 0,
                "manifest_available": False, "batch": found}
    entries = load_manifest(manifest_path)
    rows = sorted(entries.values(), key=lambda e: str(e.get("path", "")))
    failures = sum(1 for e in rows if e.get("status") == "failed")
    if failures_only:
        rows = [e for e in rows if e.get("status") == "failed"]
    total = len(rows)
    page = rows[max(0, offset):max(0, offset) + max(1, limit)]
    return {"items": [_entry_out(e) for e in page], "total": total,
            "failures": failures, "manifest_available": manifest_path.exists(),
            "batch": found}


def _entry_out(entry: dict) -> dict:
    """清单条目 → 对外投影，字段与 IMPORT_COLUMNS 一一对应（清单/CSV 看到同一
    套列，避免某条通路偷偷少一列）。缺失字段补 None：JSONL 是追加写的，
    早期版本的条目可能没有后加的键。"""
    return {col: entry.get(col) for col in IMPORT_COLUMNS}


# ---------------------------------------------------------------------------
# 停止信号处理：让"被中断"这件事变成一次正常的收尾
# ---------------------------------------------------------------------------
# 取舍：不注册信号处理时，Ctrl-C 直接冒泡成 KeyboardInterrupt，finally 仍会
# 执行（Python 保证），批次能收尾成 interrupted——但 SIGTERM（kill/容器停）
# 的默认动作是**立即终止进程**，finally 不会执行，只剩一条 running 死行。
# 所以这里把 SIGINT/SIGTERM/SIGHUP 统一转成异常，让收尾路径只有一条。
# 只能在主线程注册（CLI 场景成立），注册失败（如被嵌到别的线程）就退化成
# Python 默认行为——靠 reconcile_stale_running 兜底，不让它影响导入本身。


def _install_stop_handlers(outcome: dict) -> dict:
    """注册停止信号处理，返回原处理函数（供 _restore_stop_handlers 还原）。"""
    previous: dict = {}

    def _on_stop(signum, frame):  # noqa: ARG001 —— 签名由 signal 决定
        name = signal.Signals(signum).name
        outcome["interrupted"] = True
        outcome["error"] = f"收到 {name}，导入被中断"
        # 128+signal 是 shell 约定的"被信号终止"退出码
        outcome["exit_code"] = 128 + signum
        raise KeyboardInterrupt(f"收到 {name}")

    for sig_name in _STOP_SIGNALS:
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue                     # Windows 没有 SIGHUP
        try:
            previous[sig] = signal.signal(sig, _on_stop)
        except (ValueError, OSError):
            continue                     # 非主线程/不支持：交回默认行为
    return previous


def _restore_stop_handlers(previous: dict) -> None:
    """还原原信号处理（导入结束后不再吞 Ctrl-C）。"""
    for sig, handler in (previous or {}).items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            continue


def _os_user() -> str | None:
    """发起人：操作系统用户名（CLI 场景就是运维/交付同学）。取不到就 NULL,
    不因为拿不到用户名而拒绝导入。"""
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 —— getpass 在某些容器里会抛
        return os.environ.get("USER") or os.environ.get("USERNAME")


def _main() -> None:
    parser = argparse.ArgumentParser(description="批量导入目录到指定知识库（断点续传）")
    parser.add_argument("--kb", required=True, help="知识库 id")
    parser.add_argument("--dir", required=True, help="要导入的根目录（递归）")
    parser.add_argument("--config", default="config/kbase.yaml")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--parse-mode", default="auto", choices=["auto", "ocr"])
    parser.add_argument("--manifest", default=None,
                        help="清单文件路径（默认 data_dir/import-<kb>.jsonl，"
                             "必须在 data_dir 内）")
    parser.add_argument("--retry-failed", action="store_true",
                        help="只重跑清单里的失败项")
    parser.add_argument("--no-record", action="store_true",
                        help="不往 import_batches 记这一轮（演示/临时跑用）")
    args = parser.parse_args()

    # 复用生产装配（含 KB 级向量模型/OCR/VLM 配置），与 web 摄取语义完全一致
    from kbase.api.services import build_services
    svc = build_services(args.config)
    data_dir = Path(svc.cfg.data_dir)
    # 清单路径闸门（T18）：越界（绝对路径或含 ".."）直接拒绝——这个值会被写进
    # import_batches.manifest_path 并被读接口打开，不能让 CLI 参数换出一次
    # 任意文件读。
    raw_manifest = args.manifest or f"import-{args.kb}.jsonl"
    try:
        manifest_path = resolve_manifest_path(data_dir, str(raw_manifest))
    except ValueError as e:
        raise SystemExit(f"清单路径不合法：{e}") from e

    # 中断恢复第二条腿：同一库+同一清单的上一次运行若停在 running（进程被
    # SIGKILL/掉电，finally 没机会跑），这里先回收，不留"看着在跑其实早就死了"
    # 的死行。必须在 start_batch 之前跑，否则会把本轮自己的行也扫进去。
    if not args.no_record:
        for stale in reconcile_stale_running(svc.sf, data_dir, kb_id=args.kb,
                                             manifest_path=_manifest_rel(
                                                 data_dir, manifest_path)):
            print(f"回收上次未收尾的批次 {stale}（进程未正常结束，已标记终态）")

    root = Path(args.dir)
    batch_id = None
    if not args.no_record:
        # 先插 running 行再干活：中途掉电也留下"有这么一轮"，否则连"跑过没"
        # 都答不上来（先扫目录是为了把 total 一起记住）。
        batch_id = start_batch(
            svc.sf, args.kb, manifest_path, data_dir, started_by=_os_user(),
            context={"workers": args.workers, "parse_mode": args.parse_mode},
            dir_path=str(root), total=None)

    started = time.time()
    try:
        files = scan_files(root)
        manifest = load_manifest(manifest_path)
        pending = plan_pending(files, manifest, retry_failed=args.retry_failed)
        print(f"发现 {len(files)} 个文件，本轮待处理 {len(pending)} 个"
              f"（清单: {manifest_path}）")
        if not pending:
            print("没有需要处理的文件（全部已完成，或用 --retry-failed 重跑失败项）")
            if batch_id:
                finish_batch(svc.sf, batch_id, {"done": 0, "failed": 0},
                             status=ST_DONE,
                             context={"workers": args.workers,
                                      "parse_mode": args.parse_mode},
                             total=0)
            return
        stats = run_import(svc.pipeline, svc.sf, args.kb, pending, manifest_path,
                           workers=args.workers, parse_mode=args.parse_mode,
                           batch_id=batch_id, manages_batch=True)
        print(f"完成：成功 {stats['done']}，失败 {stats['failed']}，"
              f"耗时 {stats['elapsed_s']}s；失败项可用 --retry-failed 重跑")
    except BaseException as e:  # noqa: BLE001 —— 含 Ctrl-C/SIGTERM 转成的 KeyboardInterrupt
        # run_import 的 finally 已经把批次写成 interrupted；这里只补"扫描/装配
        # 阶段"就失败的情况（那时还没进 run_import）。批次行必须有个终态。
        if batch_id:
            finish_batch(svc.sf, batch_id, {"done": 0, "failed": 0},
                         status=ST_FAILED, error=f"{type(e).__name__}: {e}",
                         context={"workers": args.workers,
                                  "parse_mode": args.parse_mode},
                         total=0, interrupted=isinstance(e, KeyboardInterrupt),
                         exit_code=(130 if isinstance(e, KeyboardInterrupt) else 0))
        print(f"导入失败或中断（耗时 {time.time() - started:.1f}s）："
              f"{type(e).__name__}: {e}", flush=True)
        raise SystemExit(130 if isinstance(e, KeyboardInterrupt) else 1) from e


if __name__ == "__main__":
    _main()
