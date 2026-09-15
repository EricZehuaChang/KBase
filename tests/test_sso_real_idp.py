"""真机 IdP 行为的**离线**回归（CI 里没有 Keycloak，所以用实拍夹具驱动）。

背景：M6-8 的 SSO 测试一直只打桩 `discover` / `exchange_code`，从没跟真 IdP
说过话。2026-09-15 对真 Keycloak 26.0 跑通了完整授权码流（见
`scripts/dev/verify_sso_real_idp.sh`），期间撞到 4 个只有真 IdP 才会暴露的问题：
PKCE 缺失、登录 CSRF、同名账号提权、IdP 错误原文被吞（详见 tests/test_sso.py
里对应的回归用例）。

本文件的职责**不是**再跑一遍真机，而是：把真机实拍到的报文字节固化成夹具，
用它们驱动与真机同一份代码路径，让 CI 在没有 Keycloak 的环境下也能钉住
"我们依赖的 IdP 行为"——真 Keycloak 升级换字段、或我们改成读别的 claim，
这些用例会红。

夹具的再生成（在 kbase-test 上，Keycloak 起着）：
    bash scripts/dev/verify_sso_real_idp.sh --dump-fixture
    # 产物：tests/fixtures/keycloak_real_idp.json

夹具里每次都会变/涉及凭据的字段（token、refresh_token、session_state、at_hash、
sub UUID）由 dump 脚本洗成固定占位符，因此它是逐字节稳定的，可以直接当回归基线。
"""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from kbase.auth import oidc
from kbase.config import SsoConfig
from tests.test_api import CFG, FakeLLM

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "keycloak_real_idp.json"

# 真机实拍：Keycloak 的 client 一开"强制 PKCE"，而 authorize 请求没带
# code_challenge 时，它就是 302 回这个（不是 400 网页）
REAL_PKCE_REDIRECT_ERROR = {
    "error": "invalid_request",
    "error_description": "Missing parameter: code_challenge_method",
}
# 真机实拍：client_secret 错/没设时 token 端点的 401 正文
REAL_BAD_SECRET_ERROR = {
    "error": "unauthorized_client",
    "error_description": "Invalid client or Invalid client credentials",
}

SSO_CFG = CFG + """
sso:
  enabled: true
  issuer: https://idp.corp.example
  client_id: kbase
  default_role: viewer
"""


@pytest.fixture(scope="module")
def real() -> dict:
    """真机实拍夹具。"""
    assert FIXTURE_PATH.exists(), f"缺夹具 {FIXTURE_PATH}（见本文件顶部说明）"
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clear_discovery_cache():
    """discovery 是进程级缓存：用例里换 issuer/端点必须清，否则互相串味。"""
    oidc._discovery_cache.clear()
    yield
    oidc._discovery_cache.clear()


@pytest.fixture
def real_discovery(real) -> dict:
    """把夹具里的 discovery 子集当成 IdP 元数据用。"""
    meta = dict(real["discovery_subset"])
    return meta


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
    """替掉 oidc 模块里的 httpx：回放实拍响应，同时记下我们发出去的东西。"""

    HTTPError = httpx.HTTPError

    def __init__(self, post_resp, get_resp=None):
        self._post, self._get = post_resp, get_resp
        self.posted: dict = {}
        self.get_headers: dict = {}
        self.get_url = ""

    def post(self, url, timeout=None, data=None):    # noqa: A003
        self.posted = dict(data or {})
        self.posted["__url__"] = url
        return self._post

    def get(self, url, timeout=None, headers=None):  # noqa: A003
        self.get_url = url
        self.get_headers = dict(headers or {})
        return self._get


def _app(tmp_path, fake_embedder, cfg_text, monkeypatch):
    monkeypatch.setenv("KBASE_ADMIN_PASSWORD", "admin-pw")
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(cfg_text.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    return create_app(config_path=cfg, embedder=fake_embedder,
                      llms={"fake": FakeLLM()}, reranker=False, auth="on")


# --------------------------------------------------------------------------
# 1) discovery / authorize：真 Keycloak 的形状决定了我们必须带 PKCE
# --------------------------------------------------------------------------

def test_real_discovery_shape(real, real_discovery):
    """钉住 discovery 里我们用到的字段确实存在、且 issuer 与配置口径一致。

    真机结论：issuer 就是 `http://127.0.0.1:8090/realms/kbase`——**不带斜杠、
    含 /realms/<realm> 前缀**。配 sso.issuer 时多一个斜杠或少一段都会让
    discovery 404 或让 redirect_uri 对不上。"""
    assert real["issuer"] == real_discovery["issuer"]
    for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
        assert real_discovery[key].startswith(real["issuer"])
    # client_secret_post 在支持列表里 → KBase 用 client_secret 走 POST body 是对的
    assert "client_secret_post" in real_discovery[
        "token_endpoint_auth_methods_supported"]


def test_authorize_url_satisfies_pkce_required_idp(real_discovery, monkeypatch):
    """Keycloak 强制 PKCE 时，authorize 必须带 code_challenge + S256。

    真机实拍：夹具的 `code_challenge_methods_supported` 里有 S256，而测试 client
    又被配成 `pkce.code.challenge.method=S256`——不带挑战就被打回
    `error=invalid_request&error_description=Missing parameter: code_challenge_method`。"""
    monkeypatch.setattr(oidc, "discover", lambda issuer: dict(real_discovery))
    sso = SsoConfig(issuer=real_discovery["issuer"], client_id="kbase")
    redirect_uri = "http://127.0.0.1:8092/api/auth/sso/callback"
    verifier, challenge = oidc.make_pkce_pair()
    url = oidc.build_authorize_url(sso, redirect_uri, "st4te", challenge)

    q = parse_qs(urlparse(url).query)
    assert url.startswith(real_discovery["authorization_endpoint"])
    assert q["code_challenge"] == [challenge]
    assert q["code_challenge_method"] == ["S256"]
    assert q["client_id"] == ["kbase"]
    assert q["redirect_uri"] == [redirect_uri]      # 必须逐字节等于登记值
    assert q["state"] == ["st4te"]
    assert "S256" in real_discovery["code_challenge_methods_supported"]
    # 挑战是从 verifier 派生的，不是随手编的（否则 IdP 换 token 时会拒）
    import base64
    import hashlib
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    assert expected == challenge


# --------------------------------------------------------------------------
# 2) token / userinfo：用实拍报文体走一遍 exchange_code
# --------------------------------------------------------------------------

def test_exchange_code_accepts_real_keycloak_shapes(real, real_discovery,
                                                    monkeypatch):
    """把实拍的 token 响应 + userinfo 原样回放，exchange_code 必须走通并返回
    userinfo 本体；同时确认 token 请求带上了 client_secret 与 PKCE verifier。"""
    monkeypatch.setenv("KBASE_OIDC_CLIENT_SECRET", "s3cr3t")
    monkeypatch.setattr(oidc, "discover", lambda issuer: dict(real_discovery))
    fake = _FakeHttpx(_FakeResp(200, real["token_response"]),
                      _FakeResp(200, real["userinfo"]))
    monkeypatch.setattr(oidc, "httpx", fake)

    sso = SsoConfig(issuer=real_discovery["issuer"], client_id="kbase")
    info = oidc.exchange_code(sso, "the-code",
                              "http://127.0.0.1:8092/api/auth/sso/callback",
                              "verifier-xyz")

    assert info == real["userinfo"]
    assert fake.posted["__url__"] == real_discovery["token_endpoint"]
    assert fake.posted["grant_type"] == "authorization_code"
    assert fake.posted["client_secret"] == "s3cr3t"
    assert fake.posted["code_verifier"] == "verifier-xyz"
    assert fake.get_url == real_discovery["userinfo_endpoint"]
    assert fake.get_headers["Authorization"] == "Bearer <ACCESS_TOKEN>"


def test_real_userinfo_maps_to_expected_identity(real):
    """claim 映射：真 Keycloak 的 userinfo 里
    preferred_username=zhang.san、sub=UUID。登录名取 preferred_username，
    身份键取 sub——两者不能混用（混用会让 IdP 侧改名拆出两个账号）。"""
    userinfo = real["userinfo"]
    assert oidc.resolve_username(userinfo) == "zhang.san"
    assert oidc.identity_subject(userinfo) == userinfo["sub"]
    assert oidc.identity_subject(userinfo) != oidc.resolve_username(userinfo)


def test_real_userinfo_without_email(real):
    """夹具里那个 `no.email` 用户的形状：Keycloak 仍会给 preferred_username，
    email 字段整个不出现（不是空串）。此时不能因为 email 缺失就取不到登录名。"""
    userinfo = {k: v for k, v in real["userinfo"].items() if k != "email"}
    assert "email" not in userinfo
    assert oidc.resolve_username(userinfo) == "zhang.san"
    # email_verified 也在（Keycloak 在 profile 作用域下会给），但 KBase 不据此
    # 做任何判定——见交付说明里的已知缺口
    assert "email_verified" in real["userinfo"]


# --------------------------------------------------------------------------
# 3) 真 Keycloak 的错误报文必须原样透出来
# --------------------------------------------------------------------------

def test_real_bad_client_secret_error_is_surfaced(real_discovery, monkeypatch):
    """client_secret 没设/设错是首次部署最常见的失败。真机实拍 Keycloak 回
    401 + `unauthorized_client / Invalid client or Invalid client credentials`，
    这条信息必须进异常，否则运维只看到一句 HTTP 401。"""
    monkeypatch.setenv("KBASE_OIDC_CLIENT_SECRET", "wrong")
    monkeypatch.setattr(oidc, "discover", lambda issuer: dict(real_discovery))
    monkeypatch.setattr(oidc, "httpx",
                        _FakeHttpx(_FakeResp(401, REAL_BAD_SECRET_ERROR)))
    sso = SsoConfig(issuer=real_discovery["issuer"], client_id="kbase")
    with pytest.raises(oidc.OidcError) as ei:
        oidc.exchange_code(sso, "c", "http://127.0.0.1:8092/api/auth/sso/callback")
    msg = str(ei.value)
    assert "401" in msg
    assert "unauthorized_client" in msg
    assert "Invalid client or Invalid client credentials" in msg


def test_real_pkce_missing_error_is_surfaced_to_user(tmp_path, fake_embedder,
                                                      monkeypatch, real_discovery):
    """真机实拍：强制 PKCE 而 authorize 没带 code_challenge 时，Keycloak 把用户
    302 回回调地址并带 `error=invalid_request&error_description=Missing parameter:
    code_challenge_method`。改造前 KBase 只会说"回调参数无效（state 校验失败或缺
    code）"，把真正原因埋掉。这里走完整 app，钉住用户能看到 IdP 原话。"""
    monkeypatch.setattr(oidc, "discover", lambda issuer: dict(real_discovery))
    monkeypatch.setattr(oidc, "exchange_code",
                        lambda *a, **k: {"sub": "s", "preferred_username": "x"})
    app = _app(tmp_path, fake_embedder, SSO_CFG, monkeypatch)
    c = TestClient(app)
    state = parse_qs(urlparse(
        c.get("/api/auth/sso/login", follow_redirects=False).headers["location"]
    ).query)["state"][0]

    from urllib.parse import urlencode
    r = c.get("/api/auth/sso/callback?" + urlencode(
        {**REAL_PKCE_REDIRECT_ERROR, "state": state}), follow_redirects=False)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "error.sso_idp_error"
    assert detail["params"]["reason"] == "Missing parameter: code_challenge_method"
    assert "Missing parameter: code_challenge_method" in detail["message"]


# --------------------------------------------------------------------------
# 4) 身份绑定：真 sub 决定"是不是同一个人"
# --------------------------------------------------------------------------

def test_real_sub_drives_account_binding_across_rename(tmp_path, fake_embedder,
                                                       monkeypatch, real):
    """用实拍夹具里的真 sub，让同一个 IdP 用户改两次名登录：必须落在同一个
    KBase 账号上（身份键 = sub，不是 preferred_username）。

    真机场景很常见：企业里账号规整/改名后，按用户名匹配会凭空多出一个账号，
    历史问答与权限全部断在旧账号上。"""
    real_sub = real["userinfo"]["sub"]     # 夹具已把真 UUID 洗成占位符
    monkeypatch.setattr(oidc, "discover",
                        lambda issuer: dict({"authorization_endpoint": "https://idp/a",
                                             "token_endpoint": "https://idp/t",
                                             "userinfo_endpoint": "https://idp/u"}))
    current = {"u": {"sub": real_sub, "preferred_username": "zhang.san",
                     "email": "zhang.san@corp.example"}}
    monkeypatch.setattr(oidc, "exchange_code",
                        lambda sso, code, uri, code_verifier="": dict(current["u"]))

    app = _app(tmp_path, fake_embedder, SSO_CFG, monkeypatch)

    def login() -> str:
        c = TestClient(app)
        state = parse_qs(urlparse(
            c.get("/api/auth/sso/login",
                  follow_redirects=False).headers["location"]).query)["state"][0]
        r = c.get(f"/api/auth/sso/callback?code=x&state={state}",
                  follow_redirects=False)
        assert r.status_code in (302, 307), r.text
        return c.get("/api/auth/me").json()["username"]

    assert login() == "zhang.san"
    current["u"] = {"sub": real_sub, "preferred_username": "zhang.san2",
                    "email": "zhang.san2@corp.example"}
    assert login() == "zhang.san"          # 改名后仍回到原账号

    admin = TestClient(app)
    admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pw"})
    names = {u["username"] for u in admin.get("/api/users").json()}
    assert "zhang.san" in names and "zhang.san2" not in names


def test_real_repeated_login_reuses_account(tmp_path, fake_embedder, monkeypatch,
                                           real):
    """同一身份反复登录（真机日常路径）不能重复建号，也不能被
    "同名账号冲突"的保护误伤——绑定记录必须让它第二次就命中。"""
    monkeypatch.setattr(oidc, "discover",
                        lambda issuer: {"authorization_endpoint": "https://idp/a",
                                        "token_endpoint": "https://idp/t",
                                        "userinfo_endpoint": "https://idp/u"})
    monkeypatch.setattr(oidc, "exchange_code",
                        lambda sso, code, uri, code_verifier="": dict(real["userinfo"]))
    app = _app(tmp_path, fake_embedder, SSO_CFG, monkeypatch)
    for _ in range(3):
        c = TestClient(app)
        state = parse_qs(urlparse(
            c.get("/api/auth/sso/login",
                  follow_redirects=False).headers["location"]).query)["state"][0]
        r = c.get(f"/api/auth/sso/callback?code=x&state={state}",
                  follow_redirects=False)
        assert r.status_code in (302, 307), r.text
    admin = TestClient(app)
    admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pw"})
    assert sum(1 for u in admin.get("/api/users").json()
               if u["username"] == "zhang.san") == 1
