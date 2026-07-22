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
    assert r.json()["detail"]["code"] == "error.default_provider_undeletable"
    # 切换默认后原 active 即可删除
    c.put("/api/settings/active-provider", json={"name": "fake2"})
    assert c.delete("/api/settings/providers/fake").status_code == 200


def test_provider_connectivity_test_endpoint(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.post("/api/settings/providers/fake/test").json()
    assert r["ok"] is True and "latency_ms" in r


def test_provider_direct_api_key_masked_in_listing(tmp_path, fake_embedder):
    """M5-2：页面直配 api_key 存 DB；GET 列表永不返回原文，只返回
    has_api_key + 尾4位提示。"""
    c = _client(tmp_path, fake_embedder)
    r = c.post("/api/settings/providers", json={
        "name": "keyed", "base_url": "http://x/v1",
        "api_key": "sk-secret-value-abcd", "model": "m"})
    assert r.status_code == 200
    got = next(p for p in c.get("/api/settings/providers").json()["providers"]
               if p["name"] == "keyed")
    assert "api_key" not in got                       # 原文永不出站
    assert got["has_api_key"] is True
    assert got["api_key_hint"] == "****abcd"
    # 未配直配 key 的种子 provider：has_api_key=False，hint 为空
    seeded = next(p for p in c.get("/api/settings/providers").json()["providers"]
                  if p["name"] == "fake")
    assert seeded["has_api_key"] is False and seeded["api_key_hint"] is None


def test_provider_api_key_update_and_clear(tmp_path, fake_embedder):
    """PATCH 语义：不传 api_key 不动；传新值覆盖；传 "" 清除（回退 env）。"""
    c = _client(tmp_path, fake_embedder)
    c.post("/api/settings/providers", json={
        "name": "kp", "base_url": "http://x/v1",
        "api_key": "sk-first-key-1111", "model": "m"})
    # 只改 model，key 不动
    c.put("/api/settings/providers/kp", json={"model": "m2"})
    got = next(p for p in c.get("/api/settings/providers").json()["providers"]
               if p["name"] == "kp")
    assert got["has_api_key"] is True and got["model"] == "m2"
    # 覆盖 key
    c.put("/api/settings/providers/kp", json={"api_key": "sk-second-key-2222"})
    got = next(p for p in c.get("/api/settings/providers").json()["providers"]
               if p["name"] == "kp")
    assert got["api_key_hint"] == "****2222"
    # 清除 key
    c.put("/api/settings/providers/kp", json={"api_key": ""})
    got = next(p for p in c.get("/api/settings/providers").json()["providers"]
               if p["name"] == "kp")
    assert got["has_api_key"] is False


def test_provider_create_requires_some_key_source(tmp_path, fake_embedder):
    """api_key 与 api_key_env 至少给一个，否则 422（建出来也用不了，前置拦截）。"""
    c = _client(tmp_path, fake_embedder)
    r = c.post("/api/settings/providers", json={
        "name": "nokey", "base_url": "http://x/v1", "model": "m"})
    assert r.status_code == 422


def test_document_original_download(tmp_path, fake_embedder):
    """M5-2：下载识别前的原始文件——字节与上传一致，文件名恢复原名；
    source_path 失效时如实 404。"""
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    raw = MD.encode("utf-8")
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", raw, "text/markdown"))])
    doc = c.get(f"/api/kb/{kb_id}/documents").json()[0]

    r = c.get(f"/api/documents/{doc['id']}/original")
    assert r.status_code == 200
    assert r.content == raw                              # 原件字节一致
    # Content-Disposition 恢复用户上传时的原名（RFC 5987 filename* 编码中文）
    assert "attachment" in r.headers["content-disposition"]
    from urllib.parse import quote
    assert quote("补贴办法.md") in r.headers["content-disposition"]

    # 原件被清理后如实 404（不能拿 Markdown 冒充原文）
    import shutil
    shutil.rmtree(tmp_path / "data" / "uploads", ignore_errors=True)
    assert c.get(f"/api/documents/{doc['id']}/original").status_code == 404


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
