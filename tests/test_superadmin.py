"""超级管理员层级（RBAC 升级）：superadmin 在管理体系之外——普通 admin
建不了/改不了/禁不了超管账号（403），超管则拥有一切权限（rank 序放行全部
require_role）。引导账号 admin 新装即超管；存量库由迁移提升。auth=on 真实鉴权。"""
import sqlite3

from fastapi.testclient import TestClient

from kbase.migrations import run_migrations
from tests.test_users_api import _create_user, _login_admin


def _login(app, username, password):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def _setup_super_and_admin(tmp_path, fake_embedder, monkeypatch):
    """引导超管（admin）+ 建一个普通 admin（对应外发的演示账号场景）。"""
    app, superc = _login_admin(tmp_path, fake_embedder, monkeypatch)
    _create_user(superc, username="demo.admin", role="admin", password="pw123456")
    adminc = _login(app, "demo.admin", "pw123456")
    return app, superc, adminc


def test_admin_cannot_create_superadmin(tmp_path, fake_embedder, monkeypatch):
    app, superc, adminc = _setup_super_and_admin(tmp_path, fake_embedder, monkeypatch)
    r = adminc.post("/api/users", json={"username": "mallory", "role": "superadmin",
                                        "password": "pw123456"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "error.superadmin_only"


def test_admin_cannot_touch_superadmin_account(tmp_path, fake_embedder, monkeypatch):
    """普通 admin 对超管账号的任何修改（改密顶号/禁用/降级）一律 403——
    外发演示 admin 无法夺权或锁死系统 owner。"""
    app, superc, adminc = _setup_super_and_admin(tmp_path, fake_embedder, monkeypatch)
    super_id = next(u["id"] for u in superc.get("/api/users").json()
                    if u["username"] == "admin")
    for payload in ({"password": "hijack99"}, {"disabled": True},
                    {"role": "viewer"}):
        r = adminc.put(f"/api/users/{super_id}", json=payload)
        assert r.status_code == 403, payload
        assert r.json()["detail"]["code"] == "error.superadmin_only"


def test_admin_cannot_promote_to_superadmin(tmp_path, fake_embedder, monkeypatch):
    """admin 把任何人（含自己）提为 superadmin → 403（授予权跟层级走）。"""
    app, superc, adminc = _setup_super_and_admin(tmp_path, fake_embedder, monkeypatch)
    self_id = next(u["id"] for u in superc.get("/api/users").json()
                   if u["username"] == "demo.admin")
    r = adminc.put(f"/api/users/{self_id}", json={"role": "superadmin"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "error.superadmin_only"


def test_superadmin_manages_admins_and_full_access(tmp_path, fake_embedder, monkeypatch):
    """超管拥有一切：过 admin 门（用户列表）、重置普通 admin 密码、提升角色。"""
    app, superc, adminc = _setup_super_and_admin(tmp_path, fake_embedder, monkeypatch)
    admin_id = next(u["id"] for u in superc.get("/api/users").json()
                    if u["username"] == "demo.admin")
    # 重置普通 admin 密码（Eric 管演示账号的现实场景）
    assert superc.put(f"/api/users/{admin_id}",
                      json={"password": "newpw789"}).status_code == 200
    _login(app, "demo.admin", "newpw789")     # 新密码可登录
    # 超管可授予 superadmin
    r = superc.put(f"/api/users/{admin_id}", json={"role": "superadmin"})
    assert r.status_code == 200 and r.json()["role"] == "superadmin"


def test_legacy_db_without_superadmin_keeps_last_admin_guard(
        tmp_path, fake_embedder, monkeypatch):
    """无超管的存量库（迁移遗漏/手工库）退守旧规则：启用 admin 不清零。"""
    app, superc, adminc = _setup_super_and_admin(tmp_path, fake_embedder, monkeypatch)
    # 场外把超管直接降为 editor（绕过 API 守卫，模拟无超管的库）
    db = tmp_path / "data" / "kbase.sqlite"
    con = sqlite3.connect(db)
    con.execute("UPDATE users SET role='editor' WHERE username='admin'")
    con.commit(); con.close()
    # 此时 demo.admin 是唯一启用 admin：自禁 → 422 last_admin（旧兜底仍在）
    self_id = next(u["id"] for u in adminc.get("/api/users").json()
                   if u["username"] == "demo.admin")
    r = adminc.put(f"/api/users/{self_id}", json={"disabled": True})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "error.last_admin"


def test_migration_promotes_bootstrap_admin_only(tmp_path):
    """数据迁移：username=admin 且 role=admin 提为 superadmin；其余 admin
    （如外发演示账号）不动；幂等可重跑。"""
    from sqlalchemy import create_engine

    from kbase.models import Base
    db = tmp_path / "mig.sqlite"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO users (id, username, password_hash, role, disabled,"
                " advanced_ui, created_at) VALUES"
                " ('u1','admin','x','admin',0,0,'2026-01-01'),"
                " ('u2','partner','x','admin',0,0,'2026-01-01')")
    con.commit(); con.close()
    run_migrations(engine)
    run_migrations(engine)      # 幂等重跑
    con = sqlite3.connect(db)
    rows = dict(con.execute("SELECT username, role FROM users").fetchall())
    con.close()
    assert rows == {"admin": "superadmin", "partner": "admin"}
