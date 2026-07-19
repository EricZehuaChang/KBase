"""连接器框架（对标#3）：CRUD/首次全量/增量跳过/内容变更重摄/prune 镜像
/调度到期/抢锁 409/删除级联/多库权重（对标#8）。飞书网络层全部打桩不出网；
TestClient 的 BackgroundTasks 同步执行——创建/手动同步的响应返回时同步
已完成，断言即时生效。"""
import json

import pytest
from fastapi.testclient import TestClient

import kbase.feishu as feishu
from kbase.api.main import create_app
from kbase.connectors import ConnectorScheduler, reset_stale_running
from kbase.models import Connector, ConnectorDoc, Document
from tests.test_api import CFG, FakeLLM


def _make_app(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    return create_app(config_path=cfg, embedder=fake_embedder,
                      llms={"fake": FakeLLM()}, reranker=False, auth="off")


def _client(tmp_path, fake_embedder):
    return TestClient(_make_app(tmp_path, fake_embedder))


def _sf_of(client: TestClient):
    """测试后门：经调度器实例拿 session factory（直查/改 DB 行）。"""
    return client.app.state.connector_scheduler._sf


@pytest.fixture
def feishu_tree(monkeypatch):
    """可变 wiki 树桩：state["docs"] = {obj_token: {title, edit_time, text}}。
    测试中直接改 state 再触发同步，模拟源侧新增/编辑/删除。"""
    state = {"docs": {
        "doc-a": {"title": "考勤制度", "edit_time": "100",
                  "text": "全员实行弹性打卡，核心时段十点到四点。"},
        "doc-b": {"title": "报销制度", "edit_time": "100",
                  "text": "差旅住宿上限每晚500元，超出自理。"},
    }}
    monkeypatch.setattr(feishu, "_get_token", lambda a, b: "fake-token")

    def fake_children(token, space_id, parent=None):
        if parent is None:
            return [{"node_token": "root", "title": "公司制度",
                     "has_child": True, "obj_type": "folder"}]
        if parent == "root":
            return [{"node_token": f"n-{k}", "title": v["title"],
                     "has_child": False, "obj_type": "docx", "obj_token": k,
                     "obj_edit_time": v["edit_time"]}
                    for k, v in state["docs"].items()]
        return []

    def fake_blocks(token, obj_token):
        doc = state["docs"][obj_token]
        return [
            {"block_id": "p", "block_type": 1, "children": ["h", "t"]},
            {"block_id": "h", "block_type": 3,
             "heading1": {"elements": [{"text_run": {"content": doc["title"]}}]}},
            {"block_id": "t", "block_type": 2,
             "text": {"elements": [{"text_run": {"content": doc["text"]}}]}},
        ]

    monkeypatch.setattr(feishu, "list_children", fake_children)
    monkeypatch.setattr(feishu, "fetch_doc_blocks", fake_blocks)
    return state


def _setup_kb_connector(c: TestClient, interval: int = 1440) -> tuple[str, dict]:
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.put("/api/settings/feishu", json={"app_id": "cli_x", "app_secret": "s"})
    conn = c.post(f"/api/kb/{kb}/connectors",
                  json={"type": "feishu", "source": "7000001", "name": "制度库",
                        "interval_minutes": interval}).json()
    return kb, conn


def test_create_requires_credentials(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    r = c.post(f"/api/kb/{kb}/connectors",
               json={"type": "feishu", "source": "7000001"})
    assert r.status_code == 409          # 前端据此就地引导配置凭据


def test_first_sync_full_import(tmp_path, fake_embedder, feishu_tree):
    c = _client(tmp_path, fake_embedder)
    kb, conn = _setup_kb_connector(c)

    listed = c.get(f"/api/kb/{kb}/connectors").json()
    assert len(listed) == 1
    row = listed[0]
    assert row["last_sync_status"] == "done"
    assert row["last_sync_stats"] == {"added": 2, "updated": 0, "skipped": 0,
                                      "pruned": 0, "failed": 0}
    assert row["doc_count"] == 2
    docs = c.get(f"/api/kb/{kb}/documents").json()
    assert {d["status"] for d in docs} == {"ready"}
    hits = c.post(f"/api/kb/{kb}/search",
                  json={"query": "住宿 500", "top_k": 5}).json()["blocks"]
    assert any("500" in h["text"] for h in hits)


def test_incremental_skip_unchanged(tmp_path, fake_embedder, feishu_tree):
    c = _client(tmp_path, fake_embedder)
    kb, conn = _setup_kb_connector(c)
    r = c.post(f"/api/connectors/{conn['id']}/sync")
    assert r.status_code == 200
    row = c.get(f"/api/kb/{kb}/connectors").json()[0]
    # 指纹全部未变：一篇正文都不拉，文档数不变
    assert row["last_sync_stats"] == {"added": 0, "updated": 0, "skipped": 2,
                                      "pruned": 0, "failed": 0}
    assert len(c.get(f"/api/kb/{kb}/documents").json()) == 2


def test_edit_time_bump_same_content_no_reingest(tmp_path, fake_embedder,
                                                 feishu_tree):
    """版本信号变但内容没变（如权限调整碰 edit_time）：只刷新指纹，
    文档行原样保留（doc_id 不变=零重摄成本）。"""
    c = _client(tmp_path, fake_embedder)
    kb, conn = _setup_kb_connector(c)
    before_ids = {d["id"] for d in c.get(f"/api/kb/{kb}/documents").json()}

    feishu_tree["docs"]["doc-a"]["edit_time"] = "200"
    c.post(f"/api/connectors/{conn['id']}/sync")
    row = c.get(f"/api/kb/{kb}/connectors").json()[0]
    assert row["last_sync_stats"]["skipped"] == 2
    assert row["last_sync_stats"]["updated"] == 0
    assert {d["id"] for d in c.get(f"/api/kb/{kb}/documents").json()} == before_ids
    # 指纹已刷新：下次同步不再拉 doc-a 正文（映射行 fingerprint=200）
    with _sf_of(c)() as s:
        m = s.query(ConnectorDoc).filter_by(source_key="doc-a").one()
        assert m.fingerprint == "200"


def test_changed_content_reingest(tmp_path, fake_embedder, feishu_tree):
    c = _client(tmp_path, fake_embedder)
    kb, conn = _setup_kb_connector(c)

    feishu_tree["docs"]["doc-b"]["edit_time"] = "300"
    feishu_tree["docs"]["doc-b"]["text"] = "差旅住宿上限调整为每晚800元。"
    c.post(f"/api/connectors/{conn['id']}/sync")
    row = c.get(f"/api/kb/{kb}/connectors").json()[0]
    assert row["last_sync_stats"] == {"added": 0, "updated": 1, "skipped": 1,
                                      "pruned": 0, "failed": 0}
    docs = c.get(f"/api/kb/{kb}/documents").json()
    assert len(docs) == 2                 # 删旧摄新，总数不变
    hits = c.post(f"/api/kb/{kb}/search",
                  json={"query": "住宿 上限", "top_k": 5}).json()["blocks"]
    joined = " ".join(h["text"] for h in hits)
    assert "800" in joined and "500" not in joined   # 旧内容彻底出库


def test_prune_mirror_semantics(tmp_path, fake_embedder, feishu_tree):
    c = _client(tmp_path, fake_embedder)
    kb, conn = _setup_kb_connector(c)

    del feishu_tree["docs"]["doc-b"]
    c.post(f"/api/connectors/{conn['id']}/sync")
    row = c.get(f"/api/kb/{kb}/connectors").json()[0]
    assert row["last_sync_stats"]["pruned"] == 1
    docs = c.get(f"/api/kb/{kb}/documents").json()
    assert len(docs) == 1 and "考勤" in docs[0]["filename"]


def test_prune_off_keeps_local_doc(tmp_path, fake_embedder, feishu_tree):
    c = _client(tmp_path, fake_embedder)
    kb, conn = _setup_kb_connector(c)
    c.put(f"/api/connectors/{conn['id']}", json={"prune": False})

    del feishu_tree["docs"]["doc-a"]
    c.post(f"/api/connectors/{conn['id']}/sync")
    row = c.get(f"/api/kb/{kb}/connectors").json()[0]
    assert row["last_sync_stats"]["pruned"] == 0
    # 文档保留（转普通文档），映射行清理（脱离同步管理）
    assert len(c.get(f"/api/kb/{kb}/documents").json()) == 2
    assert row["doc_count"] == 1


def test_manual_sync_conflict_409(tmp_path, fake_embedder, feishu_tree):
    c = _client(tmp_path, fake_embedder)
    kb, conn = _setup_kb_connector(c)
    with _sf_of(c)() as s:
        s.get(Connector, conn["id"]).last_sync_status = "running"
        s.commit()
    assert c.post(f"/api/connectors/{conn['id']}/sync").status_code == 409
    # 启动恢复：崩溃残留的 running 复位为 failed，锁释放
    assert reset_stale_running(_sf_of(c)) == 1
    assert c.post(f"/api/connectors/{conn['id']}/sync").status_code == 200


def test_scheduler_due_selection(tmp_path, fake_embedder, feishu_tree):
    """到期判定矩阵：从未同步=到期；间隔未满=不到期；满=到期；
    disabled/interval=0/running 一律不到期。"""
    from datetime import datetime, timedelta
    c = _client(tmp_path, fake_embedder)
    kb, conn = _setup_kb_connector(c, interval=60)
    sf = _sf_of(c)
    sched = ConnectorScheduler(sf, sync_fn=lambda cid: None)
    now = datetime.utcnow()

    def _set(**fields):
        with sf() as s:
            row = s.get(Connector, conn["id"])
            for k, v in fields.items():
                setattr(row, k, v)
            s.commit()

    _set(last_sync_at=None, last_sync_status=None)
    assert sched.due_connector_ids(now) == [conn["id"]]      # 从未同步
    _set(last_sync_at=now - timedelta(minutes=30), last_sync_status="done")
    assert sched.due_connector_ids(now) == []                # 间隔未满
    _set(last_sync_at=now - timedelta(minutes=61))
    assert sched.due_connector_ids(now) == [conn["id"]]      # 已过期
    _set(enabled=False)
    assert sched.due_connector_ids(now) == []                # 停用
    _set(enabled=True, interval_minutes=0)
    assert sched.due_connector_ids(now) == []                # 仅手动
    _set(interval_minutes=60, last_sync_status="running")
    assert sched.due_connector_ids(now) == []                # 同步中不叠跑


def test_delete_connector_purge_docs(tmp_path, fake_embedder, feishu_tree):
    c = _client(tmp_path, fake_embedder)
    kb, conn = _setup_kb_connector(c)
    r = c.delete(f"/api/connectors/{conn['id']}", params={"purge_docs": True})
    assert r.json() == {"ok": True, "purged": True}
    assert c.get(f"/api/kb/{kb}/documents").json() == []
    assert c.get(f"/api/kb/{kb}/connectors").json() == []
    with _sf_of(c)() as s:
        assert s.query(ConnectorDoc).count() == 0


def test_delete_connector_keep_docs(tmp_path, fake_embedder, feishu_tree):
    c = _client(tmp_path, fake_embedder)
    kb, conn = _setup_kb_connector(c)
    c.delete(f"/api/connectors/{conn['id']}")
    assert len(c.get(f"/api/kb/{kb}/documents").json()) == 2   # 文档转普通保留


def test_kb_delete_cascades_connectors(tmp_path, fake_embedder, feishu_tree):
    c = _client(tmp_path, fake_embedder)
    kb, conn = _setup_kb_connector(c)
    c.delete(f"/api/kb/{kb}")
    with _sf_of(c)() as s:
        assert s.query(Connector).count() == 0
        assert s.query(ConnectorDoc).count() == 0
        assert s.query(Document).count() == 0


def test_source_immutable_and_update_fields(tmp_path, fake_embedder,
                                            feishu_tree):
    c = _client(tmp_path, fake_embedder)
    kb, conn = _setup_kb_connector(c)
    r = c.put(f"/api/connectors/{conn['id']}",
              json={"name": "新名", "interval_minutes": 30, "enabled": False})
    body = r.json()
    assert (body["name"], body["interval_minutes"], body["enabled"]) == \
        ("新名", 30, False)
    # source 不在更新 schema 内（extra=forbid → 422）
    assert c.put(f"/api/connectors/{conn['id']}",
                 json={"config": {"source": "x"}}).status_code == 422


def test_union_weight_reorders_multi_kb(tmp_path, fake_embedder):
    """对标#8：同一篇内容进两个库（假向量确定性→分数相同），
    B 库权重调高后必须整体排到 A 库前面。"""
    from kbase.db import make_session_factory
    from kbase.index.keyword import KeywordIndex
    from kbase.ingest.pipeline import IngestPipeline
    from kbase.models import KnowledgeBase
    from kbase.plugins.chunkers.structure import StructureChunker
    from kbase.plugins.vectorstores.chroma_store import ChromaStore
    from kbase.rag.retriever import Retriever

    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb-a", name="A"))
        s.add(KnowledgeBase(id="kb-b", name="B",
                            config=json.dumps({"union_weight": 5.0})))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    kw = KeywordIndex(factory)
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=64,
                                                        chunk_overlap=0),
                              fake_embedder, store, tmp_path / "files",
                              keyword_index=kw)
    md = "# 制度\n住宿报销上限每晚500元。\n"
    for kb_id in ("kb-a", "kb-b"):
        f = tmp_path / f"{kb_id}.md"
        f.write_text(md, encoding="utf-8")
        pipeline.ingest_file(kb_id, f, "制度.md")

    r = Retriever(factory, fake_embedder, store, keyword_index=kw)
    blocks = r.retrieve_multi(["kb-a", "kb-b"], "住宿报销上限", top_k=2)
    assert blocks[0].kb_id == "kb-b"      # 权重 5.0 拉到最前
    assert blocks[0].score > blocks[1].score