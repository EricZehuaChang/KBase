"""邀请用户（POST /users/{id}/invite）：维护邮箱+设置新初始密码+发送凭据
邮件（登录地址/账号/初始密码）。同步发送——失败回错误且不动密码；按账号
语言偏好选中/英文模板；超管目标对普通 admin 按不存在处理。auth=on。"""
import re
import sqlite3

from tests.test_users_api import _create_user, _login_admin


def _patch_mail(monkeypatch, sent):
    """打桩发件箱：configured=True + send_mail 捕获（invite 端点函数内
    import kbase.mailer，patch 源模块即可生效）。"""
    monkeypatch.setattr("kbase.mailer.status",
                        lambda sf: {"configured": True})
    monkeypatch.setattr(
        "kbase.mailer.send_mail",
        lambda sf, to, subject, body, html=None:
            sent.append({"to": to, "subject": subject, "body": body}))


def test_invite_sets_email_password_and_sends(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    sent = []
    _patch_mail(monkeypatch, sent)
    u = _create_user(c, username="pt.one", role="viewer").json()   # 未填邮箱

    r = c.post(f"/api/users/{u['id']}/invite",
               json={"email": "pt.one@corp.example", "password": "invite123"})
    assert r.status_code == 200 and r.json()["email"] == "pt.one@corp.example"
    assert len(sent) == 1 and sent[0]["to"] == "pt.one@corp.example"
    # 邮件含三要素：登录地址 / 账号 / 初始密码
    assert "http://" in sent[0]["body"]
    assert "pt.one" in sent[0]["body"] and "invite123" in sent[0]["body"]
    # 邮箱已落库；新初始密码可登录
    from fastapi.testclient import TestClient
    assert any(x["email"] == "pt.one@corp.example"
               for x in c.get("/api/users").json())
    login = TestClient(app).post(
        "/api/auth/login", json={"username": "pt.one", "password": "invite123"})
    assert login.status_code == 200


def test_invite_random_password_when_omitted(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    sent = []
    _patch_mail(monkeypatch, sent)
    u = _create_user(c, username="pt.two", role="viewer",
                     password="oldpw123").json()
    r = c.post(f"/api/users/{u['id']}/invite",
               json={"email": "pt.two@corp.example"})
    assert r.status_code == 200
    # 中文模板行"初始密码：<pw>"提取随机密码 → 能登录、旧密码失效
    pw = re.search(r"初始密码：(\S+)", sent[0]["body"]).group(1)
    from fastapi.testclient import TestClient
    assert TestClient(app).post("/api/auth/login", json={
        "username": "pt.two", "password": pw}).status_code == 200
    assert TestClient(app).post("/api/auth/login", json={
        "username": "pt.two", "password": "oldpw123"}).status_code == 401


def test_invite_requires_email_somewhere(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    _patch_mail(monkeypatch, [])
    u = _create_user(c, username="pt.three", role="viewer").json()
    r = c.post(f"/api/users/{u['id']}/invite", json={})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "error.invite_needs_email"


def test_invite_smtp_unconfigured_422(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    u = _create_user(c, username="pt.four", role="viewer").json()
    r = c.post(f"/api/users/{u['id']}/invite",
               json={"email": "x@corp.example"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "error.smtp_unconfigured"


def test_invite_send_failure_keeps_old_password(tmp_path, fake_embedder, monkeypatch):
    """发信失败 → 502 且密码不被重置（先发信后落库的顺序保证）。"""
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    monkeypatch.setattr("kbase.mailer.status", lambda sf: {"configured": True})

    def _boom(sf, to, subject, body, html=None):
        raise RuntimeError("smtp down")
    monkeypatch.setattr("kbase.mailer.send_mail", _boom)
    u = _create_user(c, username="pt.five", role="viewer",
                     password="keepme123").json()
    r = c.post(f"/api/users/{u['id']}/invite",
               json={"email": "pt.five@corp.example", "password": "newpw123"})
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "error.invite_send_failed"
    from fastapi.testclient import TestClient
    assert TestClient(app).post("/api/auth/login", json={
        "username": "pt.five", "password": "keepme123"}).status_code == 200


def test_invite_superadmin_target_hidden_from_admin(tmp_path, fake_embedder, monkeypatch):
    from fastapi.testclient import TestClient
    app, superc = _login_admin(tmp_path, fake_embedder, monkeypatch)
    _patch_mail(monkeypatch, [])
    _create_user(superc, username="demo.admin", role="admin", password="pw123456")
    super_id = next(x["id"] for x in superc.get("/api/users").json()
                    if x["username"] == "admin")
    adminc = TestClient(app)
    assert adminc.post("/api/auth/login", json={
        "username": "demo.admin", "password": "pw123456"}).status_code == 200
    r = adminc.post(f"/api/users/{super_id}/invite",
                    json={"email": "hijack@x.example"})
    assert r.status_code == 404      # 不可见即不存在


def test_invite_english_template_by_language_pref(tmp_path, fake_embedder, monkeypatch):
    """账号 language=en → 英文邀请模板（马来/国际伙伴场景）。"""
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    sent = []
    _patch_mail(monkeypatch, sent)
    u = _create_user(c, username="pt.six", role="viewer").json()
    db = tmp_path / "data" / "kbase.sqlite"
    con = sqlite3.connect(db)
    con.execute("UPDATE users SET language='en' WHERE username='pt.six'")
    con.commit(); con.close()
    r = c.post(f"/api/users/{u['id']}/invite",
               json={"email": "pt.six@corp.example"})
    assert r.status_code == 200
    assert sent[0]["subject"] == "Your KBase account is ready"
    assert "Initial password:" in sent[0]["body"]
