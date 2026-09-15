"""企业 SSO（M6-8 OIDC）：默认关闭零暴露、授权跳转带签名 state + PKCE、
state 绑定浏览器（防登录 CSRF）、回调换身份自动建号+发会话 cookie、state 伪造
400。oidc 网络层打桩，不出网。

真机（真 Keycloak 26.0）对接后新增的几条回归，对应本文件下半部分：
- `test_sso_refuses_to_hijack_existing_local_account`：IdP 里一个叫 admin 的
  用户原先能直接拿到 KBase 超管（真机实测验过），现在默认拒绝；
- `test_sso_login_sends_pkce_and_binds_state_cookie`：没带 code_challenge 时
  Keycloak（以及 Azure AD/Okta 等强制 PKCE 的 IdP）会直接
  `error=invalid_request`，用户根本见不到登录页；
- `test_sso_callback_requires_bound_state_cookie`：state 原先不与浏览器绑定，
  把别人浏览器上生成的回调 URL 甩给受害者即可让受害者被登录成攻击者；
- `test_exchange_code_surfaces_idp_error_body`：IdP 的 error_description 原先
  被 raise_for_status() 吞掉，运维只看到一句 401。
真机验证脚本与实拍夹具见 scripts/dev/verify_sso_real_idp.sh、
tests/test_sso_real_idp.py。"""
import httpx
import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from kbase.auth import oidc
from kbase.config import SsoConfig
from tests.test_api import CFG, FakeLLM

SSO_CFG = CFG + """
sso:
  enabled: true
  issuer: https://idp.corp.example
  client_id: kbase
  default_role: viewer
"""

# 允许 IdP 身份落到已存在的同名本地账号上（危险开关，见 SsoConfig 注释）
SSO_CFG_ALLOW_EXISTING = SSO_CFG + "  allow_existing_users: true\n"

# 真 Keycloak 的 discovery 形状（字段名取自实拍，见
# tests/fixtures/keycloak_real_idp.json 的 discovery_subset）
DISCOVERY = {
    "issuer": "https://idp.corp.example",
    "authorization_endpoint": "https://idp.corp.example/authorize",
    "token_endpoint": "https://idp.corp.example/token",
    "userinfo_endpoint": "https://idp.corp.example/userinfo",
}


def _app(tmp_path, fake_embedder, cfg_text, monkeypatch):
    monkeypatch.setenv("KBASE_ADMIN_PASSWORD", "admin-pw")
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(cfg_text.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    return create_app(config_path=cfg, embedder=fake_embedder,
                      llms={"fake": FakeLLM()}, reranker=False, auth="on")


def _patch_oidc(monkeypatch, userinfo: dict):
    """把 discover 打桩、exchange_code 换成固定 userinfo，并记录调用参数。"""
    monkeypatch.setattr(oidc, "discover", lambda issuer: dict(DISCOVERY))
    seen: dict = {}

    def fake_exchange(sso, code, redirect_uri, code_verifier=""):
        seen["code"] = code
        seen["redirect_uri"] = redirect_uri
        seen["code_verifier"] = code_verifier
        return dict(userinfo)

    monkeypatch.setattr(oidc, "exchange_code", fake_exchange)
    return seen


def _start_login(client):
    """走 /sso/login，返回 (authorize_url 的 query dict, 完整 authorize URL)。"""
    r = client.get("/api/auth/sso/login", follow_redirects=False)
    assert r.status_code in (302, 307), r.text
    loc = r.headers["location"]
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(loc).query), loc


def _cookie(client, name):
    for ck in client.cookies.jar:
        if ck.name == name:
            return ck.value
    return ""


def test_sso_disabled_by_default(tmp_path, fake_embedder, monkeypatch):
    app = _app(tmp_path, fake_embedder, CFG, monkeypatch)
    c = TestClient(app)
    assert c.get("/api/auth/sso/status").json() == {"enabled": False}
    assert c.get("/api/auth/sso/login", follow_redirects=False).status_code == 404


def test_sso_full_flow_auto_creates_user(tmp_path, fake_embedder, monkeypatch):
    # 真 IdP 的 userinfo 一定带 sub（OIDC 必填），夹具照实给
    seen = _patch_oidc(monkeypatch, {
        "sub": "02fe9f13-a034-486d-b2cc-c331d89eb7fa",
        "preferred_username": "zhang.san", "email": "zhang.san@corp.example",
        "email_verified": True})

    app = _app(tmp_path, fake_embedder, SSO_CFG, monkeypatch)
    c = TestClient(app)
    assert c.get("/api/auth/sso/status").json() == {"enabled": True}

    # 1) login 跳转到 IdP，带 client_id、签名 state 与 PKCE 挑战
    q, loc = _start_login(c)
    assert loc.startswith("https://idp.corp.example/authorize?")
    assert q["client_id"] == ["kbase"]
    assert q["response_type"] == ["code"]
    state = q["state"][0]

    # 2) 回调：自动建号 + 落会话 cookie + 跳回首页
    r2 = c.get(f"/api/auth/sso/callback?code=fake-code&state={state}",
               follow_redirects=False)
    assert r2.status_code in (302, 307), r2.text
    assert r2.headers["location"] == "/"
    assert "kbase_session" in r2.headers.get("set-cookie", "")
    # code_verifier 真的传给了 token 端点（PKCE 不是摆设）
    assert seen["code_verifier"]
    assert seen["redirect_uri"].endswith("/api/auth/sso/callback")

    # 3) 会话生效，身份为自动建的 viewer
    me = c.get("/api/auth/me").json()
    assert me == {"username": "zhang.san", "role": "viewer", "email": None,
                  "advanced_ui": False, "language": None, "tour_enabled": False}

    # 4) 再次登录复用同一账号（不重复建号）
    admin = TestClient(app)
    admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pw"})
    users = admin.get("/api/users").json()
    assert sum(1 for u in users if u["username"] == "zhang.san") == 1


def test_sso_forged_state_rejected(tmp_path, fake_embedder, monkeypatch):
    _patch_oidc(monkeypatch, {"sub": "s", "preferred_username": "x"})
    app = _app(tmp_path, fake_embedder, SSO_CFG, monkeypatch)
    c = TestClient(app)
    r = c.get("/api/auth/sso/callback?code=x&state=forged.state",
              follow_redirects=False)
    assert r.status_code == 400


def test_sso_login_sends_pkce_and_binds_state_cookie(tmp_path, fake_embedder,
                                                     monkeypatch):
    """真机踩中：Keycloak 的 client 一开"强制 PKCE"，不带 code_challenge 的
    authorize 请求会被直接打回 error=invalid_request，用户连登录页都见不到。
    同时 /sso/login 必须落一个把 state 绑到本浏览器的 Cookie。"""
    _patch_oidc(monkeypatch, {"sub": "s", "preferred_username": "x"})
    app = _app(tmp_path, fake_embedder, SSO_CFG, monkeypatch)
    c = TestClient(app)
    q, _loc = _start_login(c)

    assert q["code_challenge_method"] == ["S256"]
    assert len(q["code_challenge"][0]) >= 43          # RFC 7636 §4.2
    assert len(q["state"][0]) > 20                    # 签名 state 仍然在 URL 里
    token = _cookie(c, oidc.STATE_COOKIE_NAME)
    assert token, "未落 state Cookie，回调将无法与浏览器绑定"
    assert token != q["state"][0], "Cookie 里应是签名信封，不是裸 state"


def test_sso_callback_requires_bound_state_cookie(tmp_path, fake_embedder,
                                                  monkeypatch):
    """登录 CSRF 回归：攻击者在自己浏览器走完登录、把回调 URL 甩给受害者，
    受害者的浏览器上没有那个 state Cookie，必须拒绝。

    真机实测过：改造前全新浏览器（零 Cookie）打开别人的回调 URL，能拿到
    一份属于那个用户的 kbase_session。"""
    _patch_oidc(monkeypatch, {"sub": "s", "preferred_username": "zhang.san"})
    app = _app(tmp_path, fake_embedder, SSO_CFG, monkeypatch)

    attacker = TestClient(app)
    q, _ = _start_login(attacker)
    state = q["state"][0]

    victim = TestClient(app)          # 全新浏览器，没有任何 Cookie
    r = victim.get(f"/api/auth/sso/callback?code=stolen&state={state}",
                   follow_redirects=False)
    assert r.status_code == 400, r.text
    assert "未绑定到本浏览器" in r.text
    assert victim.get("/api/auth/me").status_code == 401


def test_sso_callback_state_cookie_from_other_login_rejected(tmp_path,
                                                             fake_embedder,
                                                             monkeypatch):
    """state 与 Cookie 必须成对：拿自己那次登录的 Cookie 配别人/别次的 state
    也不行（否则绑定形同虚设）。"""
    _patch_oidc(monkeypatch, {"sub": "s", "preferred_username": "zhang.san"})
    app = _app(tmp_path, fake_embedder, SSO_CFG, monkeypatch)
    c = TestClient(app)
    state_a = _start_login(c)[0]["state"][0]
    state_b = _start_login(c)[0]["state"][0]
    assert state_a != state_b
    # 浏览器里现在只有 B 次的 Cookie，配 A 次的 state → 拒绝
    r = c.get(f"/api/auth/sso/callback?code=x&state={state_a}",
              follow_redirects=False)
    assert r.status_code == 400
    assert "未绑定到本浏览器" in r.text


def test_sso_refuses_to_hijack_existing_local_account(tmp_path, fake_embedder,
                                                      monkeypatch):
    """**真机实测确认的提权路径**：IdP 里建一个 preferred_username=admin 的
    用户，登录一次就直接拿到 KBase 超管的会话（role=superadmin）。

    create_app(auth="on") 会拉起本地 bootstrap admin（superadmin），
    这里让 IdP 声称同名的 admin：默认必须拒绝，且不能发出任何会话。"""
    _patch_oidc(monkeypatch, {
        "sub": "de6f0fad-ad06-4209-9c3a-b05a293b3620",
        "preferred_username": "admin", "email": "admin@corp.example"})
    app = _app(tmp_path, fake_embedder, SSO_CFG, monkeypatch)
    c = TestClient(app)
    state = _start_login(c)[0]["state"][0]

    r = c.get(f"/api/auth/sso/callback?code=x&state={state}",
              follow_redirects=False)
    assert r.status_code == 403, r.text
    assert "同名账号" in r.text
    assert "kbase_session" not in r.headers.get("set-cookie", "")
    assert c.get("/api/auth/me").status_code == 401      # 没拿到任何身份

    # 本地 admin 的角色没被改动，也仍然是原密码登录的那一个
    admin = TestClient(app)
    assert admin.post("/api/auth/login",
                      json={"username": "admin", "password": "admin-pw"}
                      ).json()["role"] == "superadmin"


def test_sso_allow_existing_users_opt_in(tmp_path, fake_embedder, monkeypatch):
    """预置账号部署的逃生口：显式打开 allow_existing_users 后，IdP 身份才允许
    落到已存在的同名本地账号上（风险回到"完全信任 IdP 的用户名空间"）。"""
    _patch_oidc(monkeypatch, {
        "sub": "de6f0fad-ad06-4209-9c3a-b05a293b3620",
        "preferred_username": "admin", "email": "admin@corp.example"})
    app = _app(tmp_path, fake_embedder, SSO_CFG_ALLOW_EXISTING, monkeypatch)
    c = TestClient(app)
    state = _start_login(c)[0]["state"][0]
    r = c.get(f"/api/auth/sso/callback?code=x&state={state}",
              follow_redirects=False)
    assert r.status_code in (302, 307), r.text
    assert c.get("/api/auth/me").json()["role"] == "superadmin"


def test_sso_identity_follows_sub_across_rename(tmp_path, fake_embedder,
                                                monkeypatch):
    """claim 映射回归：身份键用 sub，不是 preferred_username。

    IdP 侧改名（结婚改名 / 账号规整，企业里很常见）后如果按用户名匹配，同一个
    人会变成两个 KBase 账号、历史数据断掉。绑定在 sub 上则仍然落在原账号。"""
    monkeypatch.setattr(oidc, "discover", lambda issuer: dict(DISCOVERY))
    current = {"userinfo": {"sub": "sub-1", "preferred_username": "zhang.san",
                            "email": "zhang.san@corp.example"}}
    monkeypatch.setattr(oidc, "exchange_code",
                        lambda sso, code, uri, code_verifier="": dict(current["userinfo"]))

    app = _app(tmp_path, fake_embedder, SSO_CFG, monkeypatch)
    c1 = TestClient(app)
    s1 = _start_login(c1)[0]["state"][0]
    assert c1.get(f"/api/auth/sso/callback?code=x&state={s1}",
                  follow_redirects=False).status_code in (302, 307)
    assert c1.get("/api/auth/me").json()["username"] == "zhang.san"

    # IdP 侧改名，sub 不变
    current["userinfo"] = {"sub": "sub-1", "preferred_username": "zhang.san2",
                           "email": "zhang.san2@corp.example"}
    c2 = TestClient(app)
    s2 = _start_login(c2)[0]["state"][0]
    assert c2.get(f"/api/auth/sso/callback?code=y&state={s2}",
                  follow_redirects=False).status_code in (302, 307)
    # 仍然落在原来那个账号上，没有新建 zhang.san2
    assert c2.get("/api/auth/me").json()["username"] == "zhang.san"
    admin = TestClient(app)
    admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pw"})
    names = {u["username"] for u in admin.get("/api/users").json()}
    assert "zhang.san2" not in names


def test_sso_idp_error_is_surfaced(tmp_path, fake_embedder, monkeypatch):
    """IdP 用 error/error_description 回绝时（RFC 6749 §4.1.2.1），必须原样
    告诉用户/运维，而不是笼统说"缺 code"。

    真机实拍：Keycloak 的 client 强制 PKCE 而客户端没带 code_challenge 时，
    authorize 会把用户 302 回
    `?error=invalid_request&error_description=Missing+parameter%3A+code_challenge_method`。"""
    _patch_oidc(monkeypatch, {"sub": "s", "preferred_username": "x"})
    app = _app(tmp_path, fake_embedder, SSO_CFG, monkeypatch)
    c = TestClient(app)
    state = _start_login(c)[0]["state"][0]
    r = c.get("/api/auth/sso/callback"
              f"?error=invalid_request&error_description=Missing+parameter%3A+"
              f"code_challenge_method&state={state}", follow_redirects=False)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "error.sso_idp_error"
    assert "Missing parameter: code_challenge_method" in detail["message"]


class _FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeHttpx:
    """替掉 oidc 模块里的 httpx，记录请求体并回放真机响应的形状。"""

    HTTPError = httpx.HTTPError

    def __init__(self, post_resp, get_resp=None):
        self._post = post_resp
        self._get = get_resp
        self.posted: dict = {}
        self.get_headers: dict = {}

    def post(self, url, timeout=None, data=None):    # noqa: A003
        self.posted = dict(data or {})
        self.posted["__url__"] = url
        return self._post

    def get(self, url, timeout=None, headers=None):  # noqa: A003
        self.get_headers = dict(headers or {})
        return self._get


def test_exchange_code_surfaces_idp_error_body(monkeypatch):
    """token 端点 401 时，Keycloak 的
    {"error":"unauthorized_client","error_description":"Invalid client or Invalid
    client credentials"} 必须出现在异常里——client_secret 没设/设错是首次部署
    最常见的失败，原先只得到一句 HTTP 401 加一条 MDN 链接。"""
    monkeypatch.setenv("KBASE_OIDC_CLIENT_SECRET", "wrong")
    monkeypatch.setattr(oidc, "discover", lambda issuer: dict(DISCOVERY))
    fake = _FakeHttpx(_FakeResp(401, {
        "error": "unauthorized_client",
        "error_description": "Invalid client or Invalid client credentials"}))
    monkeypatch.setattr(oidc, "httpx", fake)

    sso = SsoConfig(issuer="https://idp.corp.example", client_id="kbase")
    with pytest.raises(oidc.OidcError) as ei:
        oidc.exchange_code(sso, "the-code", "https://app/cb", "verifier-xyz")
    msg = str(ei.value)
    assert "unauthorized_client" in msg
    assert "Invalid client or Invalid client credentials" in msg
    # PKCE 的 code_verifier 确实进了 token 请求体
    assert fake.posted["code_verifier"] == "verifier-xyz"
    assert fake.posted["code"] == "the-code"
    assert fake.posted["client_secret"] == "wrong"


def test_exchange_code_surfaces_userinfo_error_body(monkeypatch):
    """userinfo 401（access_token 被 IdP 拒）也一样要带原文。"""
    monkeypatch.setenv("KBASE_OIDC_CLIENT_SECRET", "s3cr3t")
    monkeypatch.setattr(oidc, "discover", lambda issuer: dict(DISCOVERY))
    fake = _FakeHttpx(
        _FakeResp(200, {"access_token": "at", "token_type": "Bearer",
                        "expires_in": 300}),
        _FakeResp(401, {"error": "invalid_token",
                        "error_description": "Token verification failed"}))
    monkeypatch.setattr(oidc, "httpx", fake)

    sso = SsoConfig(issuer="https://idp.corp.example", client_id="kbase")
    with pytest.raises(oidc.OidcError) as ei:
        oidc.exchange_code(sso, "c", "https://app/cb")
    assert "invalid_token" in str(ei.value)
    assert fake.get_headers["Authorization"] == "Bearer at"


def test_exchange_code_ignores_non_json_error_body(monkeypatch):
    """IdP 网关挂掉回 HTML 时不能因为 .json() 抛错而丢掉状态码。"""
    monkeypatch.setattr(oidc, "discover", lambda issuer: dict(DISCOVERY))
    fake = _FakeHttpx(_FakeResp(502, None, "<html>Bad Gateway</html>"))
    monkeypatch.setattr(oidc, "httpx", fake)
    sso = SsoConfig(issuer="https://idp.corp.example", client_id="kbase")
    with pytest.raises(oidc.OidcError) as ei:
        oidc.exchange_code(sso, "c", "https://app/cb")
    assert "502" in str(ei.value)
    assert "Bad Gateway" in str(ei.value)


def test_discover_requires_endpoints(monkeypatch):
    """discovery 缺 authorization_endpoint（IdP 配错/挂错路径）要在拉取时就
    报清楚，而不是等 KeyError 冒到用户面前。"""
    monkeypatch.setattr(oidc.httpx, "get",
                        lambda url, timeout=None: _FakeResp(200, {"issuer": "x"}))
    oidc._discovery_cache.pop("https://idp.corp.example", None)
    with pytest.raises(oidc.OidcError) as ei:
        oidc.discover("https://idp.corp.example")
    assert "authorization_endpoint" in str(ei.value)


def test_resolve_username_falls_back_to_sub(monkeypatch):
    """IdP 不给 preferred_username/email 时退到 sub（真机上是 UUID）。"""
    assert oidc.resolve_username({"sub": "abc"}) == "abc"
    assert oidc.resolve_username({"sub": "abc", "email": "a@b.c"}) == "a@b.c"
    assert oidc.resolve_username({"sub": "abc", "preferred_username": "z"}) == "z"
    assert oidc.resolve_username({"sub": "  "}) is None
    # 身份键永远取 sub，不受上面回退顺序影响
    assert oidc.identity_subject({"sub": "abc", "preferred_username": "z"}) == "abc"
    assert oidc.identity_subject({}) == ""
