"""账号邮箱字段 + 自助修改密码：建号带邮箱、编辑改邮箱/清邮箱、
改密旧密码复核、新密码生效、旧密码失效。auth=on 真实鉴权。"""
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


def _login(app, username, password):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_user_email_crud(app_on):
    admin = _login(app_on, "admin", "admin-pw")
    u = admin.post("/api/users", json={
        "username": "zhang.san", "role": "viewer", "password": "pw123456",
        "email": "zhang.san@corp.example"}).json()
    assert u["email"] == "zhang.san@corp.example"
    assert any(x["email"] == "zhang.san@corp.example"
               for x in admin.get("/api/users").json())

    # 改邮箱 / 空串清除
    u2 = admin.put(f"/api/users/{u['id']}",
                   json={"email": "new@corp.example"}).json()
    assert u2["email"] == "new@corp.example"
    u3 = admin.put(f"/api/users/{u['id']}", json={"email": ""}).json()
    assert u3["email"] is None


def test_change_password_self_service(app_on):
    admin = _login(app_on, "admin", "admin-pw")
    admin.post("/api/users", json={"username": "li.si", "role": "viewer",
                                   "password": "oldpw123",
                                   "email": "li.si@corp.example"})
    user = _login(app_on, "li.si", "oldpw123")

    # 旧密码错 → 401，密码不变
    assert user.post("/api/auth/change-password",
                     json={"old_password": "wrong", "new_password": "newpw456"}
                     ).status_code == 401
    # 正确改密
    r = user.post("/api/auth/change-password",
                  json={"old_password": "oldpw123", "new_password": "newpw456"})
    assert r.status_code == 200 and r.json()["ok"] is True

    # 新密码可登录、旧密码失效
    _login(app_on, "li.si", "newpw456")
    bad = TestClient(app_on).post("/api/auth/login",
                                  json={"username": "li.si", "password": "oldpw123"})
    assert bad.status_code == 401
    # 新密码长度下限
    u2 = _login(app_on, "li.si", "newpw456")
    assert u2.post("/api/auth/change-password",
                   json={"old_password": "newpw456", "new_password": "123"}
                   ).status_code == 422
