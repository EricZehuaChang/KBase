"""批量导入（企业级体量）：扫描过滤、断点续传、失败重跑、真管道端到端。"""
from pathlib import Path

from kbase.bulk_import import load_manifest, plan_pending, run_import, scan_files


def _mk(root: Path, name: str, content: str = "内容") -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_scan_filters_and_sorts(tmp_path):
    _mk(tmp_path, "b.md")
    _mk(tmp_path, "sub/a.docx")
    _mk(tmp_path, "skip.exe")            # 不支持的类型
    _mk(tmp_path, "skip.tmp")
    files = scan_files(tmp_path)
    # 按全路径稳定排序：根目录文件在子目录之前（清单可比对、重跑顺序一致）
    assert [f.name for f in files] == ["b.md", "a.docx"]


class StubPipeline:
    """记录调用的假管道：按文件名注定成败（fail 开头→文档 failed）。"""

    def __init__(self, sf):
        self._sf = sf
        self.calls: list[str] = []

    def ingest_file(self, kb_id, path, name, parse_mode="auto"):
        import uuid
        from kbase.models import Document
        self.calls.append(name)
        doc_id = str(uuid.uuid4())
        status = "failed" if name.startswith("fail") else "ready"
        with self._sf() as s:
            s.add(Document(id=doc_id, kb_id=kb_id, filename=name,
                           content_hash=doc_id, status=status,
                           error="模拟解析失败" if status == "failed" else None))
            s.commit()
        return doc_id


def _sf(tmp_path):
    from kbase.db import make_session_factory
    return make_session_factory(f"sqlite:///{tmp_path}/bulk.sqlite")


def test_resume_skips_done_and_retry_failed(tmp_path):
    sf = _sf(tmp_path)
    pipeline = StubPipeline(sf)
    root = tmp_path / "docs"
    ok1, ok2, bad = _mk(root, "ok1.md"), _mk(root, "ok2.md"), _mk(root, "fail1.md")
    manifest = tmp_path / "m.jsonl"

    # 第一轮：全量跑，2 成 1 败
    files = scan_files(root)
    stats = run_import(pipeline, sf, "kb1", files, manifest, workers=2,
                       log=lambda *_: None)
    assert stats == {"done": 2, "failed": 1, "elapsed_s": stats["elapsed_s"]}
    assert sorted(pipeline.calls) == ["fail1.md", "ok1.md", "ok2.md"]

    # 第二轮（断点续传）：done 跳过；failed 默认自动带上重试
    m = load_manifest(manifest)
    assert [p.name for p in plan_pending(files, m)] == ["fail1.md"]
    # retry_failed=True：只跑失败项，**新文件不纳入**（定向修复模式）
    new_file = _mk(root, "new.md")
    files2 = scan_files(root)
    assert [p.name for p in plan_pending(files2, m, retry_failed=True)] == ["fail1.md"]
    # 默认模式：新文件+失败项都跑
    assert sorted(p.name for p in plan_pending(files2, m)) == ["fail1.md", "new.md"]

    # 内容变化的文件（mtime/size 变→清单键不同）会被重新纳入
    import os, time
    ok1.write_text("内容更新了", encoding="utf-8")
    os.utime(ok1, (time.time() + 5, time.time() + 5))
    names = sorted(p.name for p in plan_pending(scan_files(root), m))
    assert "ok1.md" in names and "ok2.md" not in names


def test_run_import_survives_pipeline_exception(tmp_path):
    """单文件抛异常不毁批次：记 failed 进清单，其余照跑。"""
    sf = _sf(tmp_path)

    class BoomPipeline(StubPipeline):
        def ingest_file(self, kb_id, path, name, parse_mode="auto"):
            if name == "boom.md":
                raise RuntimeError("硬盘炸了")
            return super().ingest_file(kb_id, path, name, parse_mode)

    root = tmp_path / "docs"
    _mk(root, "boom.md")
    _mk(root, "good.md")
    manifest = tmp_path / "m.jsonl"
    stats = run_import(BoomPipeline(sf), sf, "kb1", scan_files(root), manifest,
                       workers=1, log=lambda *_: None)
    assert stats["done"] == 1 and stats["failed"] == 1
    m = load_manifest(manifest)
    boom = next(e for e in m.values() if e["path"].endswith("boom.md"))
    assert boom["status"] == "failed" and "硬盘炸了" in boom["error"]


def test_bulk_import_real_pipeline_end_to_end(tmp_path, fake_embedder):
    """真管道小批量端到端：30 个 md 文件导入后全部 ready 且可检索。"""
    from fastapi.testclient import TestClient
    from kbase.api.main import create_app
    from tests.test_api import CFG, FakeLLM

    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    c = TestClient(app)
    kb_id = c.post("/api/kb", json={"name": "批量库"}).json()["id"]

    root = tmp_path / "corpus"
    for i in range(30):
        _mk(root, f"policy-{i:02d}.md",
            f"# 制度{i:02d}\n第{i}号文件规定：专项编号 SPEC-{1000+i} 的事项按本制度执行。")

    pipeline = app.state.test_llm and None   # 仅为可读性占位
    from kbase.api.services import build_services  # noqa: F401 —— 管道从 app 内部取
    svc_pipeline = c.app  # TestClient 的 app
    # 直接复用 app 内已装配的 pipeline/sf（与生产 CLI 同物）
    from kbase.api.routes import kb as _kb_routes  # noqa: F401
    # 通过再次 build 太重；批量导入的真实入口是 build_services——这里等价地
    # 从既有 app 走一遍：用与 CLI 相同的函数驱动
    # （app 没暴露 pipeline，改为直接再建一份共享同一 data_dir 的服务）
    svc = build_services(cfg, embedder=fake_embedder, llms={"fake": FakeLLM()},
                         reranker=False, enricher=False, rewriter=False)
    manifest = tmp_path / "m.jsonl"
    stats = run_import(svc.pipeline, svc.sf, kb_id, scan_files(root), manifest,
                       workers=4, log=lambda *_: None)
    assert stats["done"] == 30 and stats["failed"] == 0

    blocks = c.post(f"/api/kb/{kb_id}/search",
                    json={"query": "SPEC-1017", "top_k": 3}).json()["blocks"]
    assert blocks and "SPEC-1017" in blocks[0]["snippet"]
