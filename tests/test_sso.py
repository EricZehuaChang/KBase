"""企业 SSO（M6-8 OIDC）：默认关闭零暴露、授权跳转带签名 state、回调换身份
自动建号+发会话 cookie、state 伪造 400。oidc 网络层打桩，不出网。"""
import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from kbase.auth import oidc
from tests.test_api import CFG, FakeLLM

SSO_CFG = CFG + """
sso:
  enabled: true
  issuer: https://idp.corp.example
  client_id: kbase
  default_role: viewer
"""


def _app(tmp_path, fake_embedder, cfg_text, monkeypatch):
    monkeypatch.setenv("KBASE_ADMIN_PASSWORD", "admin-pw")
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(cfg_text.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    return create_app(config_path=cfg, embedder=fake_embedder,
                      llms={"fake": FakeLLM()}, reranker=False, auth="on")


def test_sso_disabled_by_default(tmp_path, fake_embedder, monkeypatch):
    app = _app(tmp_path, fake_embedder, CFG, monkeypatch)
    c = TestClient(app)
    assert c.get("/api/auth/sso/status").json() == {"enabled": False}
    assert c.get("/api/auth/sso/login", follow_redirects=False).status_code == 404


def test_sso_full_flow_auto_creates_user(tmp_path, fake_embedder, monkeypatch):
    monkeypatch.setattr(oidc, "discover", lambda issuer: {
        "authorization_endpoint": "https://idp.corp.example/authorize",
        "token_endpoint": "https://idp.corp.example/token",
        "userinfo_endpoint": "https://idp.corp.example/userinfo"})
    monkeypatch.setattr(oidc, "exchange_code", lambda sso, code, uri: {
        "preferred_username": "zhang.san", "email": "zhang.san@corp.example"})

    app = _app(tmp_path, fake_embedder, SSO_CFG, monkeypatch)
    c = TestClient(app)
    assert c.get("/api/auth/sso/status").json() == {"enabled": True}

    # 1) login 跳转到 IdP，带 client_id 与签名 state
    r = c.get("/api/auth/sso/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    loc = r.headers["location"]
    assert loc.startswith("https://idp.corp.example/authorize?")
    assert "client_id=kbase" in loc
    state = loc.split("state=")[1].split("&")[0]

    # 2) 回调：自动建号 + 落会话 cookie + 跳回首页
    r2 = c.get(f"/api/auth/sso/callback?code=fake-code&state={state}",
               follow_redirects=False)
    assert r2.status_code in (302, 307), r2.text
    assert r2.headers["location"] == "/"
    assert "kbase_session" in r2.headers.get("set-cookie", "")

    # 3) 会话生效，身份为自动建的 viewer
    me = c.get("/api/auth/me").json()
    assert me == {"username": "zhang.san", "role": "viewer", "email": None,
                  "advanced_ui": False, "language": None}

    # 4) 再次登录复用同一账号（不重复建号）
    admin = TestClient(app)
    admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pw"})
    users = admin.get("/api/users").json()
    assert sum(1 for u in users if u["username"] == "zhang.san") == 1


def test_sso_forged_state_rejected(tmp_path, fake_embedder, monkeypatch):
    monkeypatch.setattr(oidc, "discover", lambda issuer: {
        "authorization_endpoint": "https://idp/a", "token_endpoint": "https://idp/t",
        "userinfo_endpoint": "https://idp/u"})
    app = _app(tmp_path, fake_embedder, SSO_CFG, monkeypatch)
    c = TestClient(app)
    r = c.get("/api/auth/sso/callback?code=x&state=forged.state",
              follow_redirects=False)
    assert r.status_code == 400
