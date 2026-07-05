"""用户管理端点（admin）：GET /api/users 列表（不含 hash）/POST 创建（409 重名）/
PUT 部分更新（重置密码/禁用切换/角色变更），含"不能禁用或降级最后一个 admin"
不变量（422 中文 detail）。复用 tests/test_auth._client_on 起 auth="on" 应用。
"""
from tests.test_auth import _client_on


def _login_admin(tmp_path, fake_embedder, monkeypatch, password="adminpass123"):
    app, c = _client_on(tmp_path, fake_embedder, admin_password=password,
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": password})
    return app, c


def _create_user(c, username="alice", role="editor", password="pw123456"):
    return c.post("/api/users", json={"username": username, "role": role,
                                       "password": password})


def test_list_users_never_exposes_hash(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    _create_user(c, username="alice", role="editor")
    r = c.get("/api/users")
    assert r.status_code == 200
    items = r.json()
    usernames = {u["username"] for u in items}
    assert usernames == {"admin", "alice"}
    for u in items:
        assert set(u.keys()) == {"id", "username", "role", "disabled", "created_at"}
        assert "password" not in u
        assert "password_hash" not in u
        assert "hash" not in str(u).lower() or "hash" not in u


def test_create_user_ok(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    r = _create_user(c, username="bob", role="viewer", password="pw123456")
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "bob"
    assert body["role"] == "viewer"
    assert body["disabled"] is False
    assert "password" not in body
    assert "password_hash" not in body


def test_create_user_duplicate_username_409(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    _create_user(c, username="alice", role="editor")
    r = _create_user(c, username="alice", role="viewer")
    assert r.status_code == 409


def test_new_user_can_login(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    _create_user(c, username="alice", role="editor", password="editorpw123")
    from fastapi.testclient import TestClient
    anon = TestClient(app)
    r = anon.post("/api/auth/login", json={"username": "alice", "password": "editorpw123"})
    assert r.status_code == 200
    assert r.json()["role"] == "editor"


def test_update_user_reset_password_allows_login_with_new_password(
        tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    created = _create_user(c, username="alice", role="editor", password="oldpw12345").json()
    r = c.put(f"/api/users/{created['id']}", json={"password": "newpw98765"})
    assert r.status_code == 200

    from fastapi.testclient import TestClient
    anon = TestClient(app)
    old = anon.post("/api/auth/login", json={"username": "alice", "password": "oldpw12345"})
    assert old.status_code == 401
    new = anon.post("/api/auth/login", json={"username": "alice", "password": "newpw98765"})
    assert new.status_code == 200


def test_update_user_toggle_disabled(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    created = _create_user(c, username="alice", role="editor").json()
    r = c.put(f"/api/users/{created['id']}", json={"disabled": True})
    assert r.status_code == 200
    assert r.json()["disabled"] is True

    from fastapi.testclient import TestClient
    anon = TestClient(app)
    login = anon.post("/api/auth/login", json={"username": "alice", "password": "pw123456"})
    assert login.status_code == 401


def test_update_user_change_role(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    created = _create_user(c, username="alice", role="viewer").json()
    r = c.put(f"/api/users/{created['id']}", json={"role": "editor"})
    assert r.status_code == 200
    assert r.json()["role"] == "editor"


def test_cannot_disable_last_enabled_admin(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    users = c.get("/api/users").json()
    admin_id = next(u["id"] for u in users if u["username"] == "admin")
    r = c.put(f"/api/users/{admin_id}", json={"disabled": True})
    assert r.status_code == 422
    assert "不能禁用" in r.json()["detail"] or "最后一个管理员" in r.json()["detail"]


def test_cannot_demote_last_enabled_admin(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    users = c.get("/api/users").json()
    admin_id = next(u["id"] for u in users if u["username"] == "admin")
    r = c.put(f"/api/users/{admin_id}", json={"role": "editor"})
    assert r.status_code == 422
    assert "最后一个管理员" in r.json()["detail"]


def test_can_disable_admin_when_another_admin_remains(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    second = _create_user(c, username="admin2", role="admin", password="pw123456").json()
    users = c.get("/api/users").json()
    first_admin_id = next(u["id"] for u in users if u["username"] == "admin")
    r = c.put(f"/api/users/{first_admin_id}", json={"disabled": True})
    assert r.status_code == 200
    assert r.json()["disabled"] is True


def test_users_api_admin_only_editor_403(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    _create_user(c, username="alice", role="editor", password="editorpw123")

    from fastapi.testclient import TestClient
    editor_client = TestClient(app)
    editor_client.post("/api/auth/login",
                       json={"username": "alice", "password": "editorpw123"})
    assert editor_client.get("/api/users").status_code == 403
    assert _create_user(editor_client, username="carol", role="viewer").status_code == 403
