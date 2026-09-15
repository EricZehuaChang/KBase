"""OIDC 授权码流（M6-8 企业 SSO）：discovery → authorize 跳转 → code 换
token → userinfo 取身份。

只做标准协议的最小闭环（Keycloak/Authing/Azure AD/钉钉企业内应用等标准
OIDC IdP 均可对接）；不做 IdP 侧组/角色映射——新用户按 cfg.sso.default_role
落地，角色细化仍在 KBase 用户管理里调（保持单一权限事实源）。

网络调用集中在本模块（discover/exchange_code），测试打桩这两个函数即可
全程不出网。

真机对接（2026-09-15 对真 Keycloak 26.0 实测）修掉的三件事，都在下面：
1. **PKCE（S256）**：很多企业 IdP（Azure AD / Okta / Authing 等）把
   client 配成"必须带 code_challenge"，不带就直接回
   `error=invalid_request&error_description=Missing parameter: code_challenge_method`，
   用户根本见不到登录页。现在 authorize 带 code_challenge，token 换 code
   时带 code_verifier。
2. **state 与浏览器绑定**：state 只是 HMAC 签名的自包含 token，谁都验得过——
   攻击者在自己浏览器走完登录、把回调 URL 甩给受害者，受害者浏览器就会被
   静默登录成攻击者的账号（登录 CSRF）。现在 /sso/login 落一个
   HttpOnly 的 kbase_sso_state Cookie，回调必须与 state 匹配才放行。
   PKCE 的 code_verifier 也放这个 Cookie 里——放 state 里等于把它写进 URL
   和 IdP 日志，PKCE 就白做了。
3. **IdP 错误原文要透出来**：token/userinfo 返回 4xx 时，IdP 的
   `error`/`error_description`（如 invalid_client / Invalid client or Invalid
   client credentials）是排障的唯一线索，原先被 raise_for_status() 吞掉，
   运维只拿到一句 401 和一条 MDN 链接。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

# discovery 文档进程级缓存：IdP 元数据基本不变，每次登录都拉一遍纯浪费
_discovery_cache: dict[str, dict] = {}


class OidcError(RuntimeError):
    """IdP 侧的协议/配置错误。消息里带上 IdP 返回的 error/error_description
    原文——运维定位"密钥错了还是回调地址没登记"全靠它。"""


def discover(issuer: str) -> dict:
    """拉取并缓存 {issuer}/.well-known/openid-configuration。"""
    if issuer not in _discovery_cache:
        url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            resp = httpx.get(url, timeout=10.0)
        except httpx.HTTPError as e:
            raise OidcError(f"拉取 discovery 失败（{url}）: {e}") from e
        if resp.status_code != 200:
            raise OidcError(
                f"discovery 返回 {resp.status_code}（{url}）: {_idp_error(resp)}")
        try:
            meta = resp.json()
        except ValueError as e:
            raise OidcError(f"discovery 不是合法 JSON（{url}）") from e
        for key in ("authorization_endpoint", "token_endpoint"):
            if not meta.get(key):
                raise OidcError(f"discovery 缺少 {key}（{url}）")
        _discovery_cache[issuer] = meta
    return _discovery_cache[issuer]


def _idp_error(resp: httpx.Response) -> str:
    """从 IdP 的错误响应里抠出人话：OAuth2 用 error/error_description，
    个别 IdP 用 error_code/message；都不是就退回正文前 200 字。"""
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "").strip()[:200] or "(空响应)"
    if isinstance(body, dict):
        code = body.get("error") or body.get("error_code") or ""
        desc = body.get("error_description") or body.get("message") or ""
        text = " ".join(str(x) for x in (code, desc) if x)
        return text[:300] or json.dumps(body, ensure_ascii=False)[:200]
    return str(body)[:200]


# ---- PKCE（RFC 7636，S256）----
# 目的是兼容"强制 PKCE"的 IdP，顺带在 code 被中间人截获时多一层保护。

def make_pkce_pair() -> tuple[str, str]:
    """返回 (code_verifier, code_challenge)。verifier 用 token_urlsafe(64)
    得到 86 个字符，落在 RFC 7636 要求的 43–128 且字符集是 unreserved。"""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    return verifier, challenge


def build_authorize_url(sso, redirect_uri: str, state: str,
                        code_challenge: str = "") -> str:
    meta = discover(sso.issuer)
    params = {"response_type": "code", "client_id": sso.client_id,
              "redirect_uri": redirect_uri, "scope": "openid profile email",
              "state": state}
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{meta['authorization_endpoint']}?{urlencode(params)}"


def exchange_code(sso, code: str, redirect_uri: str,
                  code_verifier: str = "") -> dict:
    """code → access_token → userinfo。返回 userinfo dict
    （标准字段 preferred_username/email/sub）。"""
    meta = discover(sso.issuer)
    secret = os.environ.get(sso.client_secret_env, "")
    data = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": sso.client_id,
            "client_secret": secret}
    if code_verifier:
        data["code_verifier"] = code_verifier
    try:
        token_resp = httpx.post(meta["token_endpoint"], timeout=10.0, data=data)
    except httpx.HTTPError as e:
        raise OidcError(f"请求 token 端点失败（{meta['token_endpoint']}）: {e}") from e
    if token_resp.status_code != 200:
        raise OidcError(f"token 端点返回 {token_resp.status_code}: "
                        f"{_idp_error(token_resp)}")
    try:
        access_token = token_resp.json()["access_token"]
    except (ValueError, KeyError, TypeError) as e:
        raise OidcError("token 端点响应里没有 access_token") from e
    try:
        info_resp = httpx.get(meta["userinfo_endpoint"], timeout=10.0,
                              headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError as e:
        raise OidcError(
            f"请求 userinfo 端点失败（{meta['userinfo_endpoint']}）: {e}") from e
    if info_resp.status_code != 200:
        raise OidcError(f"userinfo 端点返回 {info_resp.status_code}: "
                        f"{_idp_error(info_resp)}")
    try:
        userinfo = info_resp.json()
    except ValueError as e:
        raise OidcError("userinfo 响应不是合法 JSON") from e
    if not isinstance(userinfo, dict):
        raise OidcError("userinfo 响应不是 JSON 对象")
    return userinfo


def resolve_username(userinfo: dict) -> str | None:
    """从 userinfo 提取登录名：preferred_username > email > sub。

    注意 sub 兜底：能拿到人名就用不着它，但某些 IdP 的 userinfo 只有 sub，
    此时登录名会是一个 UUID（丑但可用，且因为身份绑定按 sub 走，IdP 侧改名
    不会把用户拆成两个账号）。"""
    for key in ("preferred_username", "email", "sub"):
        value = userinfo.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def identity_subject(userinfo: dict) -> str:
    """SSO 身份的稳定标识 = sub（RFC 的 subject identifier）。

    **不要用 preferred_username 当身份键**：IdP 侧改名（结婚改名、账号规整）
    会让同一个人变成两个 KBase 账号，历史数据跟着断。sub 在同一 issuer 内
    终身不变。
    """
    sub = userinfo.get("sub")
    return sub.strip() if isinstance(sub, str) else ""


# ---- state 防 CSRF：HMAC 签名 + 时间戳，无需服务端存储 ----

STATE_TTL_SECONDS = 600
# 兼容旧名（既有调用方/测试用的就是这个名字）
_STATE_TTL_SECONDS = STATE_TTL_SECONDS
# /sso/login 落的 state Cookie 名；回调时必须与 state 匹配
STATE_COOKIE_NAME = "kbase_sso_state"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return _b64e(raw) + "." + _b64e(sig)


def _open(secret: str, token: str) -> dict | None:
    """验签 + 验期。任何异常一律 None（调用方只判 None，不区分原因——
    对攻击者不暴露"签名错"还是"过期"）。"""
    try:
        raw_b64, sig_b64 = token.split(".", 1)
        raw = _b64d(raw_b64)
        sig = _b64d(sig_b64)
        expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        if (time.time() - float(payload.get("t", 0))) >= STATE_TTL_SECONDS:
            return None
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def make_state(secret: str) -> str:
    return _sign(secret, {"n": secrets.token_urlsafe(8), "t": int(time.time())})


def verify_state(state: str, secret: str) -> bool:
    return _open(secret, state or "") is not None


def make_login_cookie(secret: str, state: str, code_verifier: str = "") -> str:
    """把 state 与 PKCE code_verifier 一起封进 Cookie。

    state 绑到"发起登录的那个浏览器"上：回调时这个 Cookie 必须存在且与回带
    的 state 一致，否则拒绝——挡住登录 CSRF（攻击者诱导受害者打开自己的回调
    URL，受害者就被登录成攻击者）。
    """
    return _sign(secret, {"s": state, "v": code_verifier,
                          "t": int(time.time())})


def open_login_cookie(secret: str, token: str, state: str) -> str | None:
    """Cookie 合法且与 state 一致 → 返回 code_verifier（可能为空串）；
    否则返回 None（调用方据此拒绝回调）。"""
    payload = _open(secret, token or "")
    if payload is None or payload.get("s") != state:
        return None
    verifier = payload.get("v")
    return verifier if isinstance(verifier, str) else ""
