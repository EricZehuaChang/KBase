"""账号邮箱字段 + 自助修改密码 + 忘记密码邮箱重置：建号带邮箱、编辑改邮箱/
清邮箱、改密旧密码复核、自助绑邮箱、重置链接全流程。auth=on 真实鉴权。"""
import re

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


def test_profile_email_self_service(app_on):
    """首登引导补录邮箱：me 暴露 email；PUT /api/auth/profile 自助绑定。"""
    admin = _login(app_on, "admin", "admin-pw")
    admin.post("/api/users", json={"username": "wang.wu", "role": "viewer",
                                   "password": "pw123456"})
    user = _login(app_on, "wang.wu", "pw123456")
    assert user.get("/api/auth/me").json()["email"] is None

    r = user.put("/api/auth/profile", json={"email": "wang.wu@corp.example"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert user.get("/api/auth/me").json()["email"] == "wang.wu@corp.example"


def test_forgot_reset_flow(app_on, monkeypatch):
    """忘记密码全流程：发信拿 token → 重置 → 新密码生效/旧失效 →
    token 一次性；未知账号防枚举（同样 200，不发信）。"""
    sent = []
    monkeypatch.setattr("kbase.mailer.send_mail",
                        lambda sf, to, subject, body: sent.append((to, body)))

    admin = _login(app_on, "admin", "admin-pw")
    admin.post("/api/users", json={"username": "zhao.liu", "role": "viewer",
                                   "password": "oldpw123",
                                   "email": "zhao.liu@corp.example"})
    sent.clear()    # 建号通知不算数，只看重置邮件

    anon = TestClient(app_on)
    # 未知账号：同样 200 同样文案，且不发信（防枚举）
    r = anon.post("/api/auth/forgot", json={"account": "no.such.user"})
    assert r.status_code == 200 and not sent

    # 按邮箱找回：收到带 token 的重置链接
    r = anon.post("/api/auth/forgot", json={"account": "zhao.liu@corp.example"})
    assert r.status_code == 200
    assert len(sent) == 1 and sent[0][0] == "zhao.liu@corp.example"
    token = re.search(r"reset_token=([\w\-]+)", sent[0][1]).group(1)

    # 坏 token → 400；真 token 重置成功
    assert anon.post("/api/auth/reset",
                     json={"token": "x" * 43, "new_password": "newpw789"}
                     ).status_code == 400
    assert anon.post("/api/auth/reset",
                     json={"token": token, "new_password": "newpw789"}
                     ).status_code == 200

    # 新密码可登录、旧密码失效、token 一次性（复用 → 400）
    _login(app_on, "zhao.liu", "newpw789")
    assert anon.post("/api/auth/login",
                     json={"username": "zhao.liu", "password": "oldpw123"}
                     ).status_code == 401
    assert anon.post("/api/auth/reset",
                     json={"token": token, "new_password": "another1"}
                     ).status_code == 400
