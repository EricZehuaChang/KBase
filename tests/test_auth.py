"""端到端鉴权测试（auth="on"）：登录/登出/me、Cookie 会话贯通、无凭据 401、
Origin 不匹配 403、豁免路径可达、bootstrap 首启 admin。复用 tests/test_api.py
的 CFG/FakeLLM，但每个 create_app 调用都显式 auth="on"（默认值，写出来更明确）。
"""
import logging

from fastapi.testclient import TestClient

from kbase.api.main import create_app
from kbase.auth import security
from kbase.models import User
from tests.test_api import CFG, FakeLLM


def _client_on(tmp_path, fake_embedder, *, admin_password=None, monkeypatch=None):
    if admin_password is not None:
        monkeypatch.setenv("KBASE_ADMIN_PASSWORD", admin_password)
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="on")
    return app, TestClient(app)


def test_login_ok_sets_cookie_and_returns_role(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    assert r.status_code == 200
    assert r.json() == {"username": "admin", "role": "admin"}
    assert "kbase_session" in r.cookies


def test_login_bad_password_401(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    r = c.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_login_disabled_user_401(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    from kbase.db import make_session_factory
    data_dir = tmp_path / "data"
    sf = make_session_factory(f"sqlite:///{data_dir}/kbase.sqlite")
    with sf() as s:
        s.add(User(id="u-viewer", username="viewer1",
                   password_hash=security.hash_password("pw12345"),
                   role="viewer", disabled=True))
        s.commit()
    r = c.post("/api/auth/login", json={"username": "viewer1", "password": "pw12345"})
    assert r.status_code == 401


def test_no_credentials_401_on_sample_route(tmp_path, fake_embedder):
    app, c = _client_on(tmp_path, fake_embedder)
    r = c.get("/api/kb")
    assert r.status_code == 401


def test_cookie_session_end_to_end(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    login = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    assert login.status_code == 200
    r = c.get("/api/kb")
    assert r.status_code == 200
    assert r.json() == []


def test_logout_then_401(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    assert c.get("/api/kb").status_code == 200
    logout = c.post("/api/auth/logout")
    assert logout.status_code == 200
    assert c.get("/api/kb").status_code == 401


def test_auth_me_returns_username_and_role(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    r = c.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json() == {"username": "admin", "role": "admin", "email": None}


def test_origin_mismatch_403_on_mutating_request(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    r = c.post("/api/kb", json={"name": "x"},
               headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403


def test_login_reachable_without_credentials(tmp_path, fake_embedder, monkeypatch):
    """POST /api/auth/login 本身豁免鉴权（否则无法登录）：错误凭据应拿到 401
    而不是 401（未认证）以外的语义混淆——这里只验证该端点不因缺 Cookie/Key
    而被全局 actor 依赖拦截（不是 401 unauthenticated 的 WWW-Authenticate 形式）。"""
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    r = c.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 401
    assert "WWW-Authenticate" not in r.headers    # 是登录失败 401，不是鉴权豁免拦截的 401


def test_healthz_and_spa_reachable_without_auth(tmp_path, fake_embedder):
    app, c = _client_on(tmp_path, fake_embedder)
    assert c.get("/healthz").status_code == 200
    r = c.get("/kb")     # SPA 深链接回退
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ---- M5-1 F1：双 SPA 回退（SPAStaticFiles，/admin 前缀回退到管理端 bundle）
# 三个测试用 web/index.html 与 web/admin.html 各自的 <title> 作为 marker
# 区分命中了哪个产物（两份 HTML 的 <title> 分别是 "KBase" 与
# "KBase 管理端"，见 web-app/index.html、web-app/admin.html 的注释）——这些
# 断言依赖仓库里已提交的构建产物 web/（house rule：构建产物入库），与既有
# test_spa_deep_link_serves_index / test_healthz_and_spa_reachable_without_auth
# 的做法一致。

def test_admin_route_serves_admin_html_marker(tmp_path, fake_embedder):
    app, c = _client_on(tmp_path, fake_embedder)
    r = c.get("/admin")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "KBase 管理端" in r.text


def test_root_route_serves_index_html_marker(tmp_path, fake_embedder):
    """GET / 应回退使用端 index.html，不能被 /admin 分流逻辑误伤——两份产物
    的 marker 互斥断言，防止"两个都回退成同一份"这种更隐蔽的坏法。"""
    app, c = _client_on(tmp_path, fake_embedder)
    r = c.get("/")
    assert r.status_code == 200
    assert "<title>KBase</title>" in r.text
    assert "KBase 管理端" not in r.text


def test_admin_deep_link_falls_back_to_admin_html(tmp_path, fake_embedder):
    """管理端前端路由深链接（如 /admin/kb 刷新页面）未命中真实文件时应回退
    admin.html 而不是 index.html——否则深链接刷新会加载错误的 bundle，
    管理端路由（vue-router base="/admin"）拿到的却是使用端的 JS。"""
    app, c = _client_on(tmp_path, fake_embedder)
    r = c.get("/admin/users-page-route")
    assert r.status_code == 200
    assert "KBase 管理端" in r.text


def test_api_docs_disabled_when_auth_on_enabled_when_off(tmp_path, fake_embedder):
    """生产（auth="on"）关闭 /docs /redoc /openapi.json——它们默认不鉴权，
    会把完整路由与模型 schema 暴露给未认证访问者；dev/test（auth="off"）保留。"""
    app_on, c_on = _client_on(tmp_path, fake_embedder)
    assert c_on.get("/openapi.json").status_code == 404

    off_dir = tmp_path / "off"
    off_dir.mkdir()
    cfg = off_dir / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(off_dir / "data").replace("\\", "/")),
                   encoding="utf-8")
    app_off = create_app(config_path=cfg, embedder=fake_embedder,
                         llms={"fake": FakeLLM()}, reranker=False, auth="off")
    c_off = TestClient(app_off)
    assert c_off.get("/openapi.json").status_code == 200


def test_bootstrap_admin_created_on_startup(tmp_path, fake_embedder):
    app, c = _client_on(tmp_path, fake_embedder)
    from kbase.db import make_session_factory
    sf = make_session_factory(f"sqlite:///{tmp_path}/data/kbase.sqlite")
    with sf() as s:
        users = s.query(User).all()
        assert len(users) == 1
        assert users[0].username == "admin"
        assert users[0].role == "admin"


def test_bootstrap_env_password_honored(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="explicit-pw-1",
                        monkeypatch=monkeypatch)
    r = c.post("/api/auth/login", json={"username": "admin", "password": "explicit-pw-1"})
    assert r.status_code == 200


def test_bootstrap_idempotent_across_app_restarts(tmp_path, fake_embedder, monkeypatch, caplog):
    """同一 data_dir 下重新 create_app（模拟重启）不应重复创建 admin 或再生成
    新的随机密码——第二次启动时 users 表已非空，ensure_admin 应跳过。"""
    monkeypatch.delenv("KBASE_ADMIN_PASSWORD", raising=False)
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    create_app(config_path=cfg, embedder=fake_embedder,
              llms={"fake": FakeLLM()}, reranker=False, auth="on")
    caplog.clear()    # 丢弃第一次启动产生的记录，只看第二次（"重启"）是否还打日志
    with caplog.at_level(logging.WARNING):
        create_app(config_path=cfg, embedder=fake_embedder,
                  llms={"fake": FakeLLM()}, reranker=False, auth="on")
    # 第二次启动不应再打随机密码日志（已引导过，ensure_admin 提前 return）
    assert not any("首启引导" in rec.message for rec in caplog.records)
    from kbase.db import make_session_factory
    sf = make_session_factory(f"sqlite:///{tmp_path}/data/kbase.sqlite")
    with sf() as s:
        assert s.query(User).count() == 1
