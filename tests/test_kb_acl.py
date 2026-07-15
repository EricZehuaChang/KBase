"""库级权限（M6-3）：不配公开、一配收紧、owner/admin 豁免、列表过滤、
无权访问统一 404、授权 CRUD。用 auth="on" 起真实鉴权。"""
import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM


@pytest.fixture
def app_on(tmp_path, fake_embedder, monkeypatch):
    monkeypatch.setenv("KBASE_ADMIN_PASSWORD", "admin-pw")
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    return create_app(config_path=cfg, embedder=fake_embedder,
                      llms={"fake": FakeLLM()}, reranker=False, auth="on")


def _login(username, password, app):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_public_until_granted(app_on):
    admin = _login("admin", "admin-pw", app_on)
    u1 = admin.post("/api/users", json={"username": "alice", "role": "viewer",
                                        "password": "pw1"}).json()
    admin.post("/api/users", json={"username": "bob", "role": "viewer",
                                   "password": "pw2"})
    kb = admin.post("/api/kb", json={"name": "财务库"}).json()["id"]

    alice = _login("alice", "pw1", app_on)
    bob = _login("bob", "pw2", app_on)

    # 未授权 = 公开：alice/bob 都能看到
    assert any(k["id"] == kb for k in alice.get("/api/kb").json())
    assert any(k["id"] == kb for k in bob.get("/api/kb").json())

    # 授权仅 alice → 收紧
    r = admin.put(f"/api/kb/{kb}/grants", json={"user_ids": [u1["id"]]})
    assert r.status_code == 200 and r.json()["count"] == 1
    assert any(k["id"] == kb for k in alice.get("/api/kb").json())
    assert not any(k["id"] == kb for k in bob.get("/api/kb").json())
    assert any(k["id"] == kb for k in admin.get("/api/kb").json())   # admin 恒可见

    # bob 无权 → 检索/建会话统一 404
    assert bob.post(f"/api/kb/{kb}/search", json={"query": "x"}).status_code == 404
    assert bob.post("/api/conversations", json={"kb_id": kb}).status_code == 404
    assert alice.post(f"/api/kb/{kb}/search", json={"query": "x"}).status_code == 200

    # 清空 → 恢复公开
    admin.put(f"/api/kb/{kb}/grants", json={"user_ids": []})
    assert any(k["id"] == kb for k in bob.get("/api/kb").json())


def test_owner_always_access(app_on):
    admin = _login("admin", "admin-pw", app_on)
    admin.post("/api/users", json={"username": "editor1", "role": "editor",
                                   "password": "pw"})
    other = admin.post("/api/users", json={"username": "other", "role": "editor",
                                           "password": "pw"}).json()
    editor = _login("editor1", "pw", app_on)
    kb = editor.post("/api/kb", json={"name": "我的库"}).json()["id"]

    # 授权只给 other，不给 owner —— owner 仍能访问自己建的库
    admin.put(f"/api/kb/{kb}/grants", json={"user_ids": [other["id"]]})
    assert any(k["id"] == kb for k in editor.get("/api/kb").json())
    assert editor.post(f"/api/kb/{kb}/search", json={"query": "x"}).status_code == 200


def test_grants_endpoint_404(app_on):
    admin = _login("admin", "admin-pw", app_on)
    kb = admin.post("/api/kb", json={"name": "库"}).json()["id"]
    assert admin.get(f"/api/kb/{kb}/grants").json()["grants"] == []
    assert admin.get("/api/kb/nope/grants").status_code == 404
    assert admin.put("/api/kb/nope/grants", json={"user_ids": []}).status_code == 404
