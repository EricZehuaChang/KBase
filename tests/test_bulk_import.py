"""批量导入（企业级体量）：扫描过滤、断点续传、失败重跑、真管道端到端。

T18 追加：批次账本（import_batches）的写入与收尾、只读接口（清单/明细/CSV）、
以及路径穿越的显式拒绝。
"""
from pathlib import Path

import pytest

from kbase.bulk_import import (IMPORT_COLUMNS, load_manifest, plan_pending,
                               run_import, scan_files)


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
    ok1, _ok2, _bad = _mk(root, "ok1.md"), _mk(root, "ok2.md"), _mk(root, "fail1.md")
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
    _mk(root, "new.md")   # 只写文件、不跑它——断言见下一行与 line 72 的对照
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

    # 直接复用 app 内已装配的 pipeline/sf（与生产 CLI 同物），避免再次 build：
    # 通过 build_services 再建一份共享同一 data_dir 的服务，等价于走一遍真实
    # 入口（app 本身没暴露 pipeline）。
    from kbase.api.services import build_services
    svc = build_services(cfg, embedder=fake_embedder, llms={"fake": FakeLLM()},
                         reranker=False, enricher=False, rewriter=False)
    manifest = tmp_path / "m.jsonl"
    stats = run_import(svc.pipeline, svc.sf, kb_id, scan_files(root), manifest,
                       workers=4, log=lambda *_: None)
    assert stats["done"] == 30 and stats["failed"] == 0

    blocks = c.post(f"/api/kb/{kb_id}/search",
                    json={"query": "SPEC-1017", "top_k": 3}).json()["blocks"]
    assert blocks and "SPEC-1017" in blocks[0]["snippet"]


# ---------------------------------------------------------------------------
# T18：批次账本 —— 写入 / 收尾 / 只读接口 / 路径穿越
# ---------------------------------------------------------------------------


def _batch_client(tmp_path, fake_embedder):
    """auth="off" 的功能测试客户端：批次接口的鉴权契约由 test_acl_matrix 钉。"""
    from fastapi.testclient import TestClient
    from kbase.api.main import create_app
    from tests.test_api import CFG, FakeLLM

    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app), tmp_path / "data"


def _run_with_batch(data_dir, sf, pipeline, kb_id, files, manifest, **kw):
    """按 CLI 的顺序跑一轮：start_batch → run_import(manages_batch=True)。
    data_dir=清单路径的基准（也是批次行落库时的相对基准，与生产一致）。"""
    from kbase import bulk_import as bi

    batch_id = bi.start_batch(sf, kb_id, manifest, data_dir,
                              started_by="tester",
                              context={"workers": 1, "parse_mode": "auto"})
    stats = run_import(pipeline, sf, kb_id, files, manifest, workers=1,
                       batch_id=batch_id, manages_batch=True,
                       log=lambda *_: None, **kw)
    return batch_id, stats


def test_batch_row_records_run_with_failures(tmp_path):
    """含坏文件的一轮：批次行落库 + 计数正确 + 终态 done_with_errors。"""
    from kbase import bulk_import as bi
    from kbase.models import KnowledgeBase

    sf = _sf(tmp_path)
    with sf() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    root = tmp_path / "docs"
    good, bad = _mk(root, "good.md"), _mk(root, "fail1.md")

    batch_id, stats = _run_with_batch(tmp_path, sf, StubPipeline(sf), "kb1",
                                      [good, bad], tmp_path / "m.jsonl")
    assert stats["done"] == 1 and stats["failed"] == 1

    row = bi.get_batch(sf, batch_id)
    assert row["kb_id"] == "kb1" and row["started_by"] == "tester"
    assert row["status"] == bi.ST_DONE_WITH_ERRORS
    assert row["started_at"] and row["finished_at"]
    assert row["summary"]["total"] == 2 and row["summary"]["done"] == 1
    assert row["summary"]["failed"] == 1 and row["summary"]["pending"] == 0
    # 清单路径**相对 data_dir** 落库（不是绝对路径）——读接口据此做包含校验
    assert row["manifest_path"] == "m.jsonl"


def test_batch_row_all_failed_is_failed_not_partial(tmp_path):
    """"跑完但一个都没成功"记 failed：0 成功通常是配置/环境问题，
    与"个别坏文件"的排查动作不同。"""
    from kbase import bulk_import as bi
    from kbase.models import KnowledgeBase

    sf = _sf(tmp_path)
    with sf() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    root = tmp_path / "docs"
    _mk(root, "fail-a.md")
    _mk(root, "fail-b.md")
    batch_id, _ = _run_with_batch(tmp_path, sf, StubPipeline(sf), "kb1",
                                  scan_files(root), tmp_path / "m.jsonl")
    row = bi.get_batch(sf, batch_id)
    assert row["status"] == bi.ST_FAILED and row["summary"]["failed"] == 2


def test_sigterm_is_turned_into_graceful_finish(tmp_path):
    """SIGTERM（kill/容器停）默认动作是立即终止进程——那样 finally 不执行，批次
    会永久停在 running。_install_stop_handlers 把它转成异常，收尾路径只剩一条。

    单独开子进程测真信号（不依赖线程池 shutdown 时序）：子进程收到 SIGTERM
    必须走 KeyboardInterrupt 分支，而不是被默认动作直接杀掉；还原处理函数后
    SIGTERM 必须重新生效（不能把宿主的处理方式改坏）。"""
    import subprocess
    import sys

    code = (
        "import signal, os\n"
        "from kbase import bulk_import as bi\n"
        "outcome = {'interrupted': False, 'error': None, 'exit_code': 0}\n"
        "prev = bi._install_stop_handlers(outcome)\n"
        "try:\n"
        "    os.kill(os.getpid(), signal.SIGTERM)\n"
        "    print('NO-EXCEPTION', flush=True)\n"
        "except KeyboardInterrupt:\n"
        "    print('SWALLOWED', outcome['interrupted'], outcome['exit_code'], flush=True)\n"
        "bi._restore_stop_handlers(prev)\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
        "print('SHOULD-NOT-PRINT', flush=True)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=60)
    assert "SWALLOWED True 143" in proc.stdout, proc.stdout + proc.stderr
    assert "SHOULD-NOT-PRINT" not in proc.stdout
    # 还原后 SIGTERM 按默认动作终止：返回码 -15（被 SIGTERM 杀死）
    assert proc.returncode == -15, f"returncode={proc.returncode}\n{proc.stderr}"


def test_interrupted_run_is_not_left_running(tmp_path):
    """被中断（Ctrl-C 冒泡成 KeyboardInterrupt）也必须收尾——批次永久停在
    running 是"看着在跑其实早就死了"的支持负担。"""
    from kbase import bulk_import as bi
    from kbase.models import KnowledgeBase

    sf = _sf(tmp_path)

    class InterruptPipeline(StubPipeline):
        def ingest_file(self, kb_id, path, name, parse_mode="auto"):
            if name == "boom.md":
                raise KeyboardInterrupt("模拟 Ctrl-C")
            return super().ingest_file(kb_id, path, name, parse_mode)

    with sf() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    root = tmp_path / "docs"
    _mk(root, "boom.md")
    _mk(root, "ok.md")
    batch_id = bi.start_batch(sf, "kb1", tmp_path / "m.jsonl", tmp_path,
                              context={"workers": 1})
    with pytest.raises(KeyboardInterrupt):
        run_import(InterruptPipeline(sf), sf, "kb1", scan_files(root),
                   tmp_path / "m.jsonl", workers=1, batch_id=batch_id,
                   manages_batch=True, log=lambda *_: None)

    row = bi.get_batch(sf, batch_id)
    assert row["status"] == bi.ST_INTERRUPTED and row["finished_at"]
    assert row["summary"]["interrupted"] is True
    assert "KeyboardInterrupt" in row["summary"]["error"]


def test_sigkill_leftover_running_row_is_reclaimed(tmp_path):
    """进程被 SIGKILL/掉电时 finally 不执行，库里只剩 running 死行：
    心跳停了超过阈值后由一致性回收收尾（真跑完的按计数收成终态，半路的记
    interrupted），**正在跑（心跳新鲜）的行绝不能被误判**。"""
    from datetime import datetime, timedelta

    from kbase import bulk_import as bi
    from kbase.models import ImportBatch, KnowledgeBase

    sf = _sf(tmp_path)
    with sf() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()

    def _mk_row(batch_id, counts, hb):
        with sf() as s:
            s.add(ImportBatch(id=batch_id, kb_id="kb1", manifest_path="m.jsonl",
                              status=bi.ST_RUNNING, heartbeat_at=hb,
                              started_at=hb,
                              counts=__import__("json").dumps(counts)))
            s.commit()

    old = datetime.utcnow() - timedelta(hours=1)
    _mk_row("11111111-1111-1111-1111-111111111111",
            {"total": 3, "done": 2, "failed": 1}, old)      # 真跑完了
    _mk_row("22222222-2222-2222-2222-222222222222",
            {"total": 10, "done": 1, "failed": 0}, old)     # 半路被杀
    _mk_row("33333333-3333-3333-3333-333333333333",
            {"total": 10, "done": 1, "failed": 0},
            datetime.utcnow())                              # 心跳新鲜=还在跑

    reclaimed = bi.reconcile_stale_running(sf, tmp_path)
    assert set(reclaimed) == {"11111111-1111-1111-1111-111111111111",
                              "22222222-2222-2222-2222-222222222222"}
    assert bi.get_batch(sf, "11111111-1111-1111-1111-111111111111")["status"] \
        == bi.ST_DONE_WITH_ERRORS
    assert bi.get_batch(sf, "22222222-2222-2222-2222-222222222222")["status"] \
        == bi.ST_INTERRUPTED
    # 新鲜心跳的行原样留着，仍然 running
    assert bi.get_batch(sf, "33333333-3333-3333-3333-333333333333")["status"] \
        == bi.ST_RUNNING
    # 幂等：终态行不参与第二次回收
    assert bi.reconcile_stale_running(sf, tmp_path) == []


def test_finish_batch_never_overwrites_final_state(tmp_path):
    """收尾幂等：已终态的行不被后来的收尾改写（中断收尾与一致性回收谁先跑
    都不会把对方结果改糟）。"""
    from kbase import bulk_import as bi
    from kbase.models import KnowledgeBase

    sf = _sf(tmp_path)
    with sf() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    batch_id = bi.start_batch(sf, "kb1", tmp_path / "m.jsonl", tmp_path)
    assert bi.finish_batch(sf, batch_id, {"done": 2, "failed": 0},
                           status=bi.ST_DONE, total=2) is True
    assert bi.finish_batch(sf, batch_id, {"done": 0, "failed": 9},
                           status=bi.ST_FAILED, total=9) is False
    assert bi.get_batch(sf, batch_id)["status"] == bi.ST_DONE


def test_resolve_manifest_path_rejects_traversal(tmp_path):
    """路径安全：清单路径是"写库后又被读接口打开"的值，绝不允许越出 data_dir。
    绝对路径、`..`、以及 resolve 后才暴露越界的路径三道都拒。"""
    from kbase import bulk_import as bi

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ok = bi.resolve_manifest_path(data_dir, "import-kb1.jsonl")
    assert ok == data_dir / "import-kb1.jsonl"
    # 子目录（data_dir/jobs/x.jsonl 这类）仍然允许
    assert bi.resolve_manifest_path(data_dir, "sub/m.jsonl").name == "m.jsonl"

    for bad in ("../etc/passwd", "sub/../../etc/passwd", "/etc/passwd", ""):
        with pytest.raises(ValueError):
            bi.resolve_manifest_path(data_dir, bad)
    # 符号链接指向 data_dir 之外同样被 resolve 后拒掉
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.jsonl").write_text("{}\n", encoding="utf-8")
    link = data_dir / "link.jsonl"
    try:
        link.symlink_to(outside / "secret.jsonl")
    except (OSError, NotImplementedError):
        pytest.skip("本文件系统不支持符号链接")
    with pytest.raises(ValueError):
        bi.resolve_manifest_path(data_dir, "link.jsonl")


def test_entries_filter_failures_only_and_csv_columns(tmp_path, fake_embedder):
    """只读接口：明细可按失败过滤；CSV 导出列完整（表头 = IMPORT_COLUMNS）。"""

    c, data_dir = _batch_client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "批量库"}).json()["id"]
    root = tmp_path / "corpus"
    _mk(root, "ok1.md", "内容一")
    _mk(root, "ok2.md", "内容二")
    _mk(root, "fail1.md", "内容三")

    sf = c.app.state.svc.sf
    batch_id, _ = _run_with_batch(data_dir, sf, StubPipeline(sf), kb_id,
                                  scan_files(root), data_dir / "import.jsonl")
    # 清单/单批次
    listed = c.get(f"/api/import-batches?kb_id={kb_id}").json()
    assert [b["id"] for b in listed["items"]] == [batch_id]
    detail = c.get(f"/api/import-batches/{batch_id}").json()
    assert detail["summary"]["done"] == 2 and detail["summary"]["failed"] == 1
    assert detail["manifest_available"] is True

    all_rows = c.get(f"/api/import-batches/{batch_id}/entries").json()
    assert all_rows["total"] == 3 and all_rows["failures"] == 1
    # 明细行字段与 CSV 列同源
    assert set(all_rows["items"][0]) == set(IMPORT_COLUMNS)

    only_fail = c.get(
        f"/api/import-batches/{batch_id}/entries?failures_only=true").json()
    assert only_fail["total"] == 1
    assert only_fail["items"][0]["path"].endswith("fail1.md")
    assert only_fail["items"][0]["status"] == "failed"

    # CSV：表头完整 + 每行与列数一致 + 失败行带 error 文本
    r = c.get(f"/api/import-batches/{batch_id}/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    body = r.content.decode("utf-8-sig")
    lines = [ln for ln in body.splitlines() if ln]
    assert lines[0] == ",".join(IMPORT_COLUMNS)
    assert len(lines) == 4                       # 表头 + 3 个文件
    assert all(len(ln.split(",")) >= len(IMPORT_COLUMNS) - 1 for ln in lines[1:])
    assert "fail1.md" in body and "模拟解析失败" in body

    csv_fail = c.get(
        f"/api/import-batches/{batch_id}/export.csv?failures_only=true")
    fail_lines = [ln for ln in csv_fail.content.decode("utf-8-sig").splitlines() if ln]
    assert len(fail_lines) == 2                  # 表头 + 1 个失败文件
    assert "fail1.md" in fail_lines[1]


def test_batch_endpoints_reject_traversal_batch_id(tmp_path, fake_embedder,
                                                  monkeypatch):
    """路径穿越请求被拒：用 batch_id 选批次的每个端点，收到穿越形态一律拒，
    并且**穿越串永远到不了任何文件路径拼接**（spy 钉住参数形态）。

    路由层本就不会把带斜杠的穿越串匹配到 `{batch_id}`（那些请求落到 SPA 静态
    兜底、拿不到 API 数据），所以这里断言两层：①API 路由表里没有任何一条会
    接受穿越串；②真到了处理函数也只会按"批次不存在"处理。"""
    from kbase import bulk_import as bi

    c, _data_dir = _batch_client(tmp_path, fake_embedder)
    seen: list[str] = []
    real_get_batch, real_safe = bi.get_batch, bi.safe_batch_id

    def spy_get_batch(sf, batch_id):
        seen.append(batch_id)
        assert ".." not in batch_id and "/" not in batch_id, \
            f"穿越串进了批次查询: {batch_id!r}"
        return real_get_batch(sf, batch_id)

    monkeypatch.setattr(bi, "get_batch", spy_get_batch)
    evil = ["..%2f..%2fetc%2fpasswd", "..", "%2e%2e%2fpasswd", "....//etc",
            "not-a-uuid", "%00"]
    for raw in evil:
        for suffix in ("", "/entries", "/export.csv", "/entries?failures_only=true"):
            r = c.get(f"/api/import-batches/{raw}{suffix}")
            if r.status_code == 200:
                # 唯一允许的 200 是 SPA 静态兜底（返回 index.html），
                # 不是 API 数据——绝不能出现文件内容或明细 JSON
                assert "text/html" in r.headers.get("content-type", ""), \
                    f"{raw}{suffix} → {r.status_code} {r.headers.get('content-type')}"
            else:
                assert r.status_code == 404, f"{raw}{suffix} → {r.status_code}"

    # 非 uuid 形态在函数层直接判空（不做任何拼接）；uuid 形态才放行
    assert real_safe("../../etc/passwd") == ""
    assert real_safe("..%2fpasswd") == ""
    assert real_safe("11111111-1111-1111-1111-111111111111") != ""
    assert real_get_batch(c.app.state.svc.sf, "../m.jsonl") is None
    # 路径穿越的 batch_id 一次都没进过查库（哪怕路由层放行到这里也会被挡）
    assert seen == [] or all(".." not in s for s in seen)


def test_entries_refuse_manifest_outside_data_dir(tmp_path, fake_embedder):
    """库里若存了越界的 manifest_path（历史/手工改库），读接口只回"清单不可
    读"，不得打开该文件、也不得 500。"""
    from kbase import bulk_import as bi
    from kbase.models import ImportBatch

    c, _data_dir = _batch_client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    sf = c.app.state.svc.sf
    with sf() as s:
        s.add(ImportBatch(id="44444444-4444-4444-4444-444444444444", kb_id=kb_id,
                          manifest_path="../outside.jsonl",
                          status=bi.ST_DONE, counts="{}"))
        s.commit()
    bid = "44444444-4444-4444-4444-444444444444"
    r = c.get(f"/api/import-batches/{bid}/entries")
    assert r.status_code == 200
    assert r.json()["items"] == [] and r.json()["manifest_available"] is False
    assert c.get(f"/api/import-batches/{bid}/export.csv").content.decode(
        "utf-8-sig").splitlines()[0] == ",".join(IMPORT_COLUMNS)


def test_no_http_endpoint_triggers_import(tmp_path, fake_embedder):
    """硬禁止：不提供任何触发导入的 HTTP 端点（BackgroundTasks 重启即丢，
    首轮灌库必须能断点续传——见 bulk_import.py 文件头与 routes 模块注释）。
    这条守卫会在有人加 POST /api/import-batches 时立刻变红。"""
    c, _data_dir = _batch_client(tmp_path, fake_embedder)
    for path, ops in c.app.openapi()["paths"].items():
        if not path.startswith("/api/import-batches"):
            continue
        assert set(ops) <= {"get"}, f"{path} 引入了非 GET 方法: {sorted(ops)}"
    # 触发导入只有命令行入口：模块里没有任何 fastapi 路由装饰器
    import inspect

    from kbase import bulk_import as bi
    src = inspect.getsource(bi)
    assert "APIRouter" not in src and "@router." not in src
