"""POC 演示数据一键装载（E）：建库+三篇样例可检索、幂等不重复灌。"""
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def test_demo_data_load_and_idempotent(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)

    r = c.post("/api/demo-data").json()
    assert r["created"] is True and r["name"] == "演示知识库"
    assert len(r["accepted"]) == 3
    kb_id = r["id"]

    # 三篇都摄取成功（TestClient 的 BackgroundTasks 同步执行完才返回）
    docs = c.get(f"/api/kb/{kb_id}/documents").json()
    assert len(docs) == 3
    assert all(d["status"] == "ready" for d in docs)

    # 演示招牌：表格行线性化——问补贴表里的一行，应能检索到
    hits = c.post(f"/api/kb/{kb_id}/search",
                  json={"query": "住房补贴 金额标准", "top_k": 5}).json()["blocks"]
    assert any("800" in b["text"] for b in hits)

    # 幂等：再点一次不重复建库
    r2 = c.post("/api/demo-data").json()
    assert r2["created"] is False and r2["id"] == kb_id
    assert len(c.get("/api/kb").json()) == 1
