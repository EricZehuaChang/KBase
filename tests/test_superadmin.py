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


def test_superadmin_invisible_to_admin(tmp_path, fake_embedder, monkeypatch):
    """超管账号对普通 admin 完全不可见：用户列表不出现；超管自己看得到全量。"""
    app, superc, adminc = _setup_super_and_admin(tmp_path, fake_embedder, monkeypatch)
    admin_view = {u["username"] for u in adminc.get("/api/users").json()}
    assert "admin" not in admin_view          # 超管行被过滤
    assert "demo.admin" in admin_view
    super_view = {u["username"] for u in superc.get("/api/users").json()}
    assert {"admin", "demo.admin"} <= super_view   # 超管看全量


def test_admin_cannot_touch_superadmin_account(tmp_path, fake_embedder, monkeypatch):
    """普通 admin 对超管账号的任何修改（改密顶号/禁用/降级）一律 404——
    不可见即不存在（403 会泄漏存在性），外发演示 admin 无法夺权或锁死 owner。"""
    app, superc, adminc = _setup_super_and_admin(tmp_path, fake_embedder, monkeypatch)
    super_id = next(u["id"] for u in superc.get("/api/users").json()
                    if u["username"] == "admin")
    for payload in ({"password": "hijack99"}, {"disabled": True},
                    {"role": "viewer"}):
        r = adminc.put(f"/api/users/{super_id}", json=payload)
        assert r.status_code == 404, payload
        assert r.json()["detail"]["code"] == "error.user_not_found"


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


def test_superadmin_audit_visible_only_to_superadmin(
        tmp_path, fake_embedder, monkeypatch):
    """审计分层：超管的操作记录只有超管自己可见；普通 admin 的审计视图里
    不出现超管 actor 的行（total 同口径），自己的行照常可见。"""
    app, superc, adminc = _setup_super_and_admin(tmp_path, fake_embedder, monkeypatch)
    # superc 建号已产生 actor=admin 的审计行；再让 adminc 产生一行自己的
    adminc.post("/api/users", json={"username": "aud.viewer", "role": "viewer",
                                    "password": "pw123456"})
    admin_view = adminc.get("/api/audit").json()
    admin_actors = {i["actor"] for i in admin_view["items"]}
    assert "admin" not in admin_actors            # 超管痕迹被过滤
    assert "demo.admin" in admin_actors           # 自己的操作可见
    assert admin_view["total"] == len(admin_view["items"])  # total 同口径

    super_view = superc.get("/api/audit").json()
    super_actors = {i["actor"] for i in super_view["items"]}
    assert {"admin", "demo.admin"} <= super_actors  # 超管看全量
    assert super_view["total"] > admin_view["total"]


def test_superadmin_can_rename_user(tmp_path, fake_embedder, monkeypatch):
    """超管改任意账号名：旧名会话失效/旧名登录失败/新名可登录；重名 409；
    普通 admin 改名 403（身份级操作仅超管）。"""
    app, superc, adminc = _setup_super_and_admin(tmp_path, fake_embedder, monkeypatch)
    u = _create_user(superc, username="old.name", role="viewer",
                     password="pw123456").json()
    userc = _login(app, "old.name", "pw123456")   # 持旧名会话

    # 普通 admin 改名 → 403
    r = adminc.put(f"/api/users/{u['id']}", json={"username": "x.name"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "error.superadmin_required"
    # 重名 → 409
    assert superc.put(f"/api/users/{u['id']}",
                      json={"username": "demo.admin"}).status_code == 409
    # 超管改名成功
    r = superc.put(f"/api/users/{u['id']}", json={"username": "new.name"})
    assert r.status_code == 200 and r.json()["username"] == "new.name"
    # 旧名会话下一次请求即 401（JWT sub=旧名查无此人）
    assert userc.get("/api/auth/me").status_code == 401
    # 旧名登录失败、新名+原密码可登录
    from fastapi.testclient import TestClient
    assert TestClient(app).post("/api/auth/login", json={
        "username": "old.name", "password": "pw123456"}).status_code == 401
    _login(app, "new.name", "pw123456")


def test_superadmin_can_delete_user_with_cascade(tmp_path, fake_embedder, monkeypatch):
    """超管删号：账号消失、登录失败；其私有数据（会话/消息/反馈/授权行）
    级联清理；普通 admin 删号 403；最后一个启用超管不可删。"""
    import sqlite3 as _sql
    from datetime import datetime as _dt

    app, superc, adminc = _setup_super_and_admin(tmp_path, fake_embedder, monkeypatch)
    u = _create_user(superc, username="gone.user", role="viewer",
                     password="pw123456").json()
    # 直插该用户的私有数据（会话+消息+反馈+库授权行）
    db = str(tmp_path / "data" / "kbase.sqlite")
    con = _sql.connect(db)
    now = _dt.utcnow().isoformat(sep=" ")
    con.execute("INSERT INTO conversations (id, kb_id, title, user_id,"
                " created_at, updated_at) VALUES ('cv1','kbx','t',?,?,?)",
                (u["id"], now, now))
    con.execute("INSERT INTO messages (id, conv_id, seq, role, content,"
                " created_at) VALUES ('m1','cv1',1,'user','q',?)", (now,))
    con.execute("INSERT INTO message_feedback (id, message_id, conv_id,"
                " rating, created_at) VALUES ('fb1','m1','cv1',1,?)", (now,))
    con.execute("INSERT INTO kb_grants (id, kb_id, user_id, created_at)"
                " VALUES ('g1','kbx',?,?)", (u["id"], now))
    con.commit(); con.close()

    # 普通 admin 删号 → 403
    r = adminc.delete(f"/api/users/{u['id']}")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "error.superadmin_required"

    # 超管删号成功：账号与私有数据全清，登录 401
    assert superc.delete(f"/api/users/{u['id']}").json()["ok"] is True
    assert "gone.user" not in {x["username"]
                               for x in superc.get("/api/users").json()}
    con = _sql.connect(db)
    for table in ("conversations", "messages", "message_feedback", "kb_grants"):
        assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    con.close()
    from fastapi.testclient import TestClient
    assert TestClient(app).post("/api/auth/login", json={
        "username": "gone.user", "password": "pw123456"}).status_code == 401

    # 最后一个启用超管不可删（防锁死）
    super_id = next(x["id"] for x in superc.get("/api/users").json()
                    if x["username"] == "admin")
    r = superc.delete(f"/api/users/{super_id}")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "error.last_superadmin"


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


def test_tour_whitelist(tmp_path, fake_embedder, monkeypatch):
    """产品导览白名单：超管恒 True；普通 admin 默认 False；进 AppSetting
    `tour_allowed_users` 白名单后 True——按账号控制，不随角色。"""
    import json as _json

    from kbase.db import make_session_factory
    from kbase.models import AppSetting

    app, superc, adminc = _setup_super_and_admin(tmp_path, fake_embedder, monkeypatch)
    assert superc.get("/api/auth/me").json()["tour_enabled"] is True
    assert adminc.get("/api/auth/me").json()["tour_enabled"] is False

    sf = make_session_factory(f"sqlite:///{tmp_path}/data/kbase.sqlite")
    with sf() as s:
        s.add(AppSetting(key="tour_allowed_users",
                         value=_json.dumps(["demo.admin"])))
        s.commit()
    assert adminc.get("/api/auth/me").json()["tour_enabled"] is True
