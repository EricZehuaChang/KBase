"""kbase/auth/deps.py 单测：get_current_actor（Cookie JWT / Bearer API Key 双通道）、
require_role 角色序校验、origin_guard 中间件。用最小 FastAPI app 直接挂依赖测试，
不依赖完整 kbase create_app（那部分留给 test_auth.py 端到端覆盖）。"""
import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from kbase.auth import security
from kbase.auth.deps import make_get_current_actor, make_origin_guard_middleware, require_role
from kbase.db import make_session_factory
from kbase.models import ApiKey, User

SECRET = "test-secret"


def _make_app(sf):
    app = FastAPI()
    get_current_actor = make_get_current_actor(sf, secret=SECRET)

    @app.get("/whoami")
    def whoami(actor=Depends(get_current_actor)):
        return {"name": actor["name"], "role": actor["role"]}

    @app.get("/viewer-ok")
    def viewer_ok(actor=Depends(require_role(get_current_actor, "viewer"))):
        return {"ok": True}

    @app.get("/editor-only")
    def editor_only(actor=Depends(require_role(get_current_actor, "editor"))):
        return {"ok": True}

    @app.get("/admin-only")
    def admin_only(actor=Depends(require_role(get_current_actor, "admin"))):
        return {"ok": True}

    app.middleware("http")(make_origin_guard_middleware())

    @app.post("/mutate")
    def mutate():
        return {"ok": True}

    return app


@pytest.fixture
def sf(tmp_path):
    return make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")


@pytest.fixture
def client(sf):
    return TestClient(_make_app(sf))


def _add_user(sf, username="alice", role="editor", disabled=False, password="pw123456"):
    with sf() as s:
        s.add(User(id=str(uuid.uuid4()), username=username,
                   password_hash=security.hash_password(password),
                   role=role, disabled=disabled))
        s.commit()


def _add_api_key(sf, role="viewer", revoked=False, name="mcp"):
    full_key, prefix, key_hash = security.generate_api_key()
    with sf() as s:
        s.add(ApiKey(id=str(uuid.uuid4()), name=name, prefix=prefix,
                     key_hash=key_hash, role=role, revoked=revoked))
        s.commit()
    return full_key


# ---- get_current_actor: Cookie JWT ----

def test_cookie_session_resolves_user(client, sf):
    _add_user(sf, username="alice", role="editor")
    token = security.create_session_token("alice", "editor", secret=SECRET)
    client.cookies.set("kbase_session", token)
    r = client.get("/whoami")
    assert r.status_code == 200
    assert r.json() == {"name": "alice", "role": "editor"}


def test_cookie_session_disabled_user_rejected(client, sf):
    _add_user(sf, username="alice", role="editor", disabled=True)
    token = security.create_session_token("alice", "editor", secret=SECRET)
    client.cookies.set("kbase_session", token)
    r = client.get("/whoami")
    assert r.status_code == 401


def test_cookie_session_unknown_user_rejected(client, sf):
    token = security.create_session_token("ghost", "editor", secret=SECRET)
    client.cookies.set("kbase_session", token)
    r = client.get("/whoami")
    assert r.status_code == 401


def test_cookie_session_invalid_token_rejected(client):
    client.cookies.set("kbase_session", "not-a-jwt")
    r = client.get("/whoami")
    assert r.status_code == 401


# ---- get_current_actor: Bearer API Key ----

def test_bearer_api_key_resolves_actor(client, sf):
    full_key = _add_api_key(sf, role="viewer", name="mcp-key")
    r = client.get("/whoami", headers={"Authorization": f"Bearer {full_key}"})
    assert r.status_code == 200
    assert r.json() == {"name": "mcp-key", "role": "viewer"}


def test_bearer_revoked_api_key_rejected(client, sf):
    full_key = _add_api_key(sf, role="viewer", revoked=True)
    r = client.get("/whoami", headers={"Authorization": f"Bearer {full_key}"})
    assert r.status_code == 401


def test_bearer_unknown_api_key_rejected(client):
    r = client.get("/whoami", headers={"Authorization": "Bearer kbase_ak_bogusbogusbogusbogusbogusbogus1"})
    assert r.status_code == 401


# ---- neither channel ----

def test_no_credentials_returns_401_with_www_authenticate(client):
    r = client.get("/whoami")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


# ---- require_role hierarchy ----

def test_require_role_admin_passes_all(client, sf):
    _add_user(sf, username="root", role="admin")
    token = security.create_session_token("root", "admin", secret=SECRET)
    client.cookies.set("kbase_session", token)
    assert client.get("/viewer-ok").status_code == 200
    assert client.get("/editor-only").status_code == 200
    assert client.get("/admin-only").status_code == 200


def test_require_role_editor_blocked_from_admin(client, sf):
    _add_user(sf, username="bob", role="editor")
    token = security.create_session_token("bob", "editor", secret=SECRET)
    client.cookies.set("kbase_session", token)
    assert client.get("/viewer-ok").status_code == 200
    assert client.get("/editor-only").status_code == 200
    r = client.get("/admin-only")
    assert r.status_code == 403
    assert r.json()["detail"]     # 中文 detail 非空


def test_require_role_viewer_blocked_from_editor_and_admin(client, sf):
    _add_user(sf, username="carol", role="viewer")
    token = security.create_session_token("carol", "viewer", secret=SECRET)
    client.cookies.set("kbase_session", token)
    assert client.get("/viewer-ok").status_code == 200
    assert client.get("/editor-only").status_code == 403
    assert client.get("/admin-only").status_code == 403


# ---- origin guard middleware ----

def test_origin_guard_allows_matching_origin(client):
    r = client.post("/mutate", headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_origin_guard_blocks_mismatched_origin(client):
    r = client.post("/mutate", headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403


def test_origin_guard_allows_no_origin_header(client):
    """非浏览器客户端（无 Origin 头）放行——它们走 Bearer 通道，不依赖 CSRF 防护。"""
    r = client.post("/mutate")
    assert r.status_code == 200


def test_origin_guard_does_not_check_get_requests(client):
    r = client.get("/whoami", headers={"Origin": "http://evil.example.com"})
    assert r.status_code != 403     # 401（无凭据）而不是被 origin guard 拦成 403
