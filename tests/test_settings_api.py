from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, MD, FakeLLM


def _client(tmp_path, fake_embedder, llms=None):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms=llms or {"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def test_yaml_seeded_into_db_once(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    rows = c.get("/api/settings/providers").json()
    names = {r["name"] for r in rows["providers"]}
    assert "fake" in names and "fake2" in names            # CFG 里的两个
    assert rows["active"] == "fake"


def test_provider_crud(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.post("/api/settings/providers", json={
        "name": "new-p", "base_url": "http://x/v1",
        "api_key_env": "NEW_KEY", "model": "m2", "max_concurrency": 2,
        "params": {"extra_body": {"enable_thinking": False}}})
    assert r.status_code == 200
    got = c.get("/api/settings/providers").json()["providers"]
    new = next(p for p in got if p["name"] == "new-p")
    assert new["params"]["extra_body"]["enable_thinking"] is False
    c.put("/api/settings/providers/new-p", json={"model": "m3"})
    got = c.get("/api/settings/providers").json()["providers"]
    assert next(p for p in got if p["name"] == "new-p")["model"] == "m3"
    assert c.delete("/api/settings/providers/new-p").status_code == 200
    c.put("/api/settings/active-provider", json={"name": "fake2"})
    assert c.get("/api/settings/providers").json()["active"] == "fake2"


def test_delete_active_provider_rejected(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    assert c.get("/api/settings/providers").json()["active"] == "fake"
    r = c.delete("/api/settings/providers/fake")
    assert r.status_code == 409
    assert "不可删除" in r.json()["detail"]
    # 切换默认后原 active 即可删除
    c.put("/api/settings/active-provider", json={"name": "fake2"})
    assert c.delete("/api/settings/providers/fake").status_code == 200


def test_provider_connectivity_test_endpoint(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.post("/api/settings/providers/fake/test").json()
    assert r["ok"] is True and "latency_ms" in r


def test_document_fulltext_and_delete(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    doc = c.get(f"/api/kb/{kb_id}/documents").json()[0]
    full = c.get(f"/api/documents/{doc['id']}/content").json()
    assert "申领条件" in full["markdown"]
    assert c.delete(f"/api/kb/{kb_id}/documents/{doc['id']}").status_code == 200
    assert c.get(f"/api/kb/{kb_id}/documents").json() == []
    assert c.get(f"/api/documents/{doc['id']}/content").status_code == 404
