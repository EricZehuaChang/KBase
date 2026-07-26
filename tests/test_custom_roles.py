"""自定义角色 RBAC：超管建角色（勾权限）→ 挂到用户 → 权限真实强制
（content.manage 决定能否建库、system.admin 决定能否进设置组）。内置角色
行为零回归；角色不可与内置重名、在用不可删、未定义角色不可挂。auth=on。"""
from fastapi.testclient import TestClient

from tests.test_users_api import _create_user, _login_admin


def _login(app, username, password):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_role_crud_and_builtin_protection(tmp_path, fake_embedder, monkeypatch):
    app, superc = _login_admin(tmp_path, fake_embedder, monkeypatch)
    # 清单含内置四角色 + 权限词表
    listed = superc.get("/api/roles").json()
    names = {r["name"] for r in listed["roles"]}
    assert {"superadmin", "admin", "editor", "viewer"} <= names
    assert set(listed["permissions"]) == {"content.manage", "system.admin",
                                          "audit.view"}
    assert next(r for r in listed["roles"] if r["name"] == "editor")[
        "permissions"] == ["content.manage"]

    # 建自定义角色
    r = superc.post("/api/roles", json={
        "name": "auditor", "label": "审计员", "permissions": ["audit.view"]})
    assert r.status_code == 200 and r.json()["permissions"] == ["audit.view"]
    # 重名 409；内置名 422
    assert superc.post("/api/roles", json={"name": "auditor"}).status_code == 409
    assert superc.post("/api/roles", json={"name": "admin"}).status_code == 422
    # 改权限
    r = superc.put("/api/roles/auditor",
                   json={"permissions": ["audit.view", "content.manage"]})
    assert set(r.json()["permissions"]) == {"audit.view", "content.manage"}
    # 内置不可改/删
    assert superc.put("/api/roles/editor", json={"label": "x"}).status_code == 422
    assert superc.delete("/api/roles/viewer").status_code == 422
    # 删自定义
    assert superc.delete("/api/roles/auditor").json()["ok"] is True
    assert superc.delete("/api/roles/auditor").status_code == 404


def test_custom_role_permissions_enforced(tmp_path, fake_embedder, monkeypatch):
    """权限真实强制：只给 content.manage 的角色能建库、进不了设置组；
    只给 system.admin 的角色反之。"""
    app, superc = _login_admin(tmp_path, fake_embedder, monkeypatch)
    superc.post("/api/roles", json={"name": "creator", "label": "内容员",
                                    "permissions": ["content.manage"]})
    superc.post("/api/roles", json={"name": "sysop", "label": "系统员",
                                    "permissions": ["system.admin"]})
    _create_user(superc, username="c.one", role="creator", password="pw123456")
    _create_user(superc, username="s.one", role="sysop", password="pw123456")

    cc = _login(app, "c.one", "pw123456")
    assert cc.post("/api/kb", json={"name": "库A"}).status_code == 200   # content ✓
    assert cc.get("/api/users").status_code == 403                       # system ✗

    sc = _login(app, "s.one", "pw123456")
    assert sc.get("/api/users").status_code == 200                       # system ✓
    assert sc.post("/api/kb", json={"name": "库B"}).status_code == 403    # content ✗


def test_role_without_permissions_can_still_ask(tmp_path, fake_embedder, monkeypatch):
    """零权限自定义角色=viewer 等价：能问答（viewer 门槛只要登录），
    但建库/设置全 403。"""
    app, superc = _login_admin(tmp_path, fake_embedder, monkeypatch)
    superc.post("/api/roles", json={"name": "guest", "permissions": []})
    _create_user(superc, username="g.one", role="guest", password="pw123456")
    gc = _login(app, "g.one", "pw123456")
    assert gc.get("/api/kb").status_code == 200        # viewer 门槛：登录即可
    assert gc.post("/api/kb", json={"name": "x"}).status_code == 403
    assert gc.get("/api/users").status_code == 403


def test_cannot_assign_undefined_role(tmp_path, fake_embedder, monkeypatch):
    """未定义角色不可挂（UserRole 放开为 str 后由路由校验兜底）。"""
    app, superc = _login_admin(tmp_path, fake_embedder, monkeypatch)
    r = superc.post("/api/users", json={"username": "bad.one", "role": "ghost",
                                        "password": "pw123456"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "error.role_not_found"
    u = _create_user(superc, username="ok.one", role="viewer").json()
    assert superc.put(f"/api/users/{u['id']}",
                      json={"role": "ghost"}).status_code == 422


def test_role_in_use_cannot_be_deleted(tmp_path, fake_embedder, monkeypatch):
    app, superc = _login_admin(tmp_path, fake_embedder, monkeypatch)
    superc.post("/api/roles", json={"name": "temp", "permissions": []})
    u = _create_user(superc, username="t.one", role="temp",
                     password="pw123456").json()
    r = superc.delete("/api/roles/temp")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "error.role_in_use"
    # 改派后可删
    superc.put(f"/api/users/{u['id']}", json={"role": "viewer"})
    assert superc.delete("/api/roles/temp").json()["ok"] is True


def test_role_mutations_require_superadmin(tmp_path, fake_embedder, monkeypatch):
    """普通 admin 可看角色清单（下拉需要），但建/改/删角色 403。"""
    app, superc = _login_admin(tmp_path, fake_embedder, monkeypatch)
    superc.post("/api/roles", json={"name": "rr", "permissions": []})
    _create_user(superc, username="plain.admin", role="admin", password="pw123456")
    adminc = _login(app, "plain.admin", "pw123456")
    assert adminc.get("/api/roles").status_code == 200
    assert adminc.post("/api/roles", json={"name": "x2"}).status_code == 403
    assert adminc.put("/api/roles/rr", json={"label": "x"}).status_code == 403
    assert adminc.delete("/api/roles/rr").status_code == 403
