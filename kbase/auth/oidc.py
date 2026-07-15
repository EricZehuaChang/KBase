"""OIDC 授权码流（M6-8 企业 SSO）：discovery → authorize 跳转 → code 换
token → userinfo 取身份。

只做标准协议的最小闭环（Keycloak/Authing/Azure AD/钉钉企业内应用等标准
OIDC IdP 均可对接）；不做 IdP 侧组/角色映射——新用户按 cfg.sso.default_role
落地，角色细化仍在 KBase 用户管理里调（保持单一权限事实源）。

网络调用集中在本模块（discover/exchange_code），测试打桩这两个函数即可
全程不出网。
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


def discover(issuer: str) -> dict:
    """拉取并缓存 {issuer}/.well-known/openid-configuration。"""
    if issuer not in _discovery_cache:
        url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        _discovery_cache[issuer] = resp.json()
    return _discovery_cache[issuer]


def build_authorize_url(sso, redirect_uri: str, state: str) -> str:
    meta = discover(sso.issuer)
    params = {"response_type": "code", "client_id": sso.client_id,
              "redirect_uri": redirect_uri, "scope": "openid profile email",
              "state": state}
    return f"{meta['authorization_endpoint']}?{urlencode(params)}"


def exchange_code(sso, code: str, redirect_uri: str) -> dict:
    """code → access_token → userinfo。返回 userinfo dict
    （标准字段 preferred_username/email/sub）。"""
    meta = discover(sso.issuer)
    secret = os.environ.get(sso.client_secret_env, "")
    token_resp = httpx.post(meta["token_endpoint"], timeout=10.0, data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect_uri, "client_id": sso.client_id,
        "client_secret": secret})
    token_resp.raise_for_status()
    access_token = token_resp.json()["access_token"]
    info_resp = httpx.get(meta["userinfo_endpoint"], timeout=10.0,
                          headers={"Authorization": f"Bearer {access_token}"})
    info_resp.raise_for_status()
    return info_resp.json()


def resolve_username(userinfo: dict) -> str | None:
    """从 userinfo 提取登录名：preferred_username > email > sub。"""
    return (userinfo.get("preferred_username") or userinfo.get("email")
            or userinfo.get("sub"))


# ---- state 防 CSRF：HMAC 签名 + 时间戳，无需服务端存储 ----

_STATE_TTL_SECONDS = 600


def make_state(secret: str) -> str:
    payload = json.dumps({"n": secrets.token_urlsafe(8), "t": int(time.time())})
    raw = payload.encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return (base64.urlsafe_b64encode(raw).decode().rstrip("=") + "."
            + base64.urlsafe_b64encode(sig).decode().rstrip("="))


def verify_state(state: str, secret: str) -> bool:
    try:
        raw_b64, sig_b64 = state.split(".", 1)
        raw = base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4))
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return False
        payload = json.loads(raw)
        return (time.time() - payload["t"]) < _STATE_TTL_SECONDS
    except (ValueError, KeyError, json.JSONDecodeError):
        return False
