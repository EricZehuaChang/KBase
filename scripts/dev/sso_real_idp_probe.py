#!/usr/bin/env python
"""真机 OIDC 授权码流探针（KBase 企业 SSO M6-8 的真 IdP 验收）。

背景：M6-8 落地时所有测试都把 kbase/auth/oidc.py 的 discover / exchange_code
打桩掉了，从没跟真 IdP 说过话。本探针补这一课——**全程不打桩**，走真 HTTP：

  1. GET /api/auth/sso/status                → 后端 SSO 开关
  2. GET /api/auth/sso/login                 → 302 到真 Keycloak 的 authorize
  3. GET authorize                           → 真登录页 HTML
  4. POST 登录表单（用户名/口令）            → 302 带真 authorization code
  5. GET /api/auth/sso/callback?code=&state= → KBase 内部换 token+userinfo，
                                               落 kbase_session Cookie，302 回 "/"
  6. GET /api/auth/me   （带 Cookie）        → 200，身份 = IdP 里的用户名
  7. GET /api/kb        （带 Cookie）        → 200，真实业务接口可用

第 6/7 步的 200 + Set-Cookie 就是"真用户登进来了"的证据。

另外单独跑一遍 **直连探针**（不经过 KBase）：自己拿 code、自己换 token、
自己调 userinfo，把 Keycloak 真实返回的 JSON 形状打出来（state/nonce/sub/
preferred_username/email/email_verified 等），据此生成
tests/fixtures/keycloak_userinfo.json 这类夹具喂给 CI 里的离线回归测试。

没有 KBase 依赖也能单跑直连段：加 --direct-only。

用法（服务器上，见 scripts/dev/verify_sso_real_idp.sh）：
  python sso_real_idp_probe.py --app-url http://127.0.0.1:8092 \
      --issuer http://127.0.0.1:8090/realms/kbase \
      --client-id kbase --client-secret <secret> \
      --username zhang.san --password <pw> \
      [--dump-fixture <path>] [--direct-only]

退出码：0 全部断言通过；1 有断言失败（会打印失败的那一步）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html as html_mod
import json
import os
import re
import secrets
import sys
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

# --------------------------------------------------------------------------
# 输出：每步一行 [OK]/[FAIL]，最后打印结论。日志同时需要能贴进交付文档，所以
# 保持单行、无 ANSI 颜色。
# --------------------------------------------------------------------------
_results: list[tuple[bool, str]] = []


def step(ok: bool, msg: str) -> bool:
    _results.append((ok, msg))
    print(f"[{'OK' if ok else 'FAIL'}] {msg}", flush=True)
    return ok


# --------------------------------------------------------------------------
# Keycloak 登录页解析：只依赖官方主题里稳定的 id="kc-form-login" 与 action 属性。
# --------------------------------------------------------------------------
def parse_login_form(page_html: str) -> str:
    """从 Keycloak 登录页取出表单 POST 地址（含 session_code/execution）。

    取整个 <form ...> 标签再匹配 action，而不是写死属性顺序——Keycloak 各版本
    的 id/onsubmit/action 顺序并不固定。HTML 里 & 被转义成 &amp;，必须还原，
    否则 POST 到一个带 &amp; 的错误 URL。
    """
    for tag in re.findall(r"<form\b[^>]*>", page_html):
        if "kc-form-login" in tag:
            m = re.search(r'action="([^"]*)"', tag)
            if m:
                return html_mod.unescape(m.group(1))
    raise AssertionError("登录页里找不到 id=kc-form-login 的表单（Keycloak 主题变了？）")


def parse_error_page(page_html: str) -> str:
    """取 Keycloak 报错页里的提示文字，失败时给可读原因。"""
    m = re.search(r'id="kc-error-message".*?<p[^>]*>(.*?)</p>', page_html, re.S)
    if not m:
        m = re.search(r'class="pf-c-alert__title"[^>]*>(.*?)<', page_html, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else "(无错误提示)"


class _LoopbackClient(httpx.Client):
    """把 loopback 上的 `Secure` Cookie 照发出去——补浏览器的那条例外。

    真机第一次跑就撞上这个：Keycloak 的 AUTH_SESSION_ID / KC_RESTART 带
    `Secure; SameSite=None`（Keycloak 为支持跨站 POST 流程强制加的），而
    http.cookiejar（httpx 的 Cookie 实现）严格按 RFC 6265 —— Secure Cookie
    只在 https 上发。于是登录表单 POST 不带 Cookie，Keycloak 回 400
    "Cookie not found. Please make sure cookies are enabled in your browser."

    真实浏览器不会这样：Chrome/Firefox/Safari 都把 http://127.0.0.1 与
    http://localhost 当**安全上下文**（Secure Contexts 规范），Secure Cookie
    照样发。探针要模拟的是浏览器，所以这里补上同样的例外。

    ⚠️ 这条**不是 KBase 的 Bug**：KBase 只在服务器侧跟 IdP 说话（discovery /
    token / userinfo），不参与浏览器 Cookie 往来；但它是个真实的部署陷阱——
    生产必须让用户经 HTTPS 访问 KBase 与 IdP，否则浏览器同样发不出这个 Cookie。
    """

    def send(self, request, **kwargs):
        self._relax_loopback_secure()
        resp = super().send(request, **kwargs)
        self._relax_loopback_secure()
        return resp

    def _relax_loopback_secure(self) -> None:
        for ck in self.cookies.jar:
            if ck.secure and ck.domain in ("127.0.0.1", "localhost", "::1"):
                ck.secure = False


def _client() -> httpx.Client:
    """单个 Client 贯穿全流程：Cookie（KC_AUTH_SESSION / KEYCLOAK_IDENTITY）
    必须持续携带，否则登录页的 session_code 与 POST 对不上。"""
    return _LoopbackClient(follow_redirects=False, timeout=30.0,
                           headers={"User-Agent": "kbase-sso-real-idp-probe/1.0"})


def _log_exchange(label: str, resp: httpx.Response, body: str = "") -> None:
    print(f"    → {label}: HTTP {resp.status_code}"
          f"{' ' + body if body else ''}", flush=True)


# --------------------------------------------------------------------------
# A) 真机全流程：经过 KBase
# --------------------------------------------------------------------------
def oidc_pkce_pair() -> tuple[str, str]:
    """本地算一对 PKCE（与 kbase/auth/oidc.py 同一套 S256 口径）。

    直连探针故意不 import kbase——它要证明"KBase 之外的这条链路也通"，顺手
    把 KBase 侧的 PKCE 实现与一个独立实现交叉验证。"""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    return verifier, challenge


def _drive_login(c, app_url: str, username: str, password: str) -> dict:
    """替某个浏览器走完前四步，返回 {authorize_url, state, callback_url}。

    第 5 步（回调）故意留给调用方：正向流程要"同一个浏览器"接着走，负向流程
    要换个浏览器走——两种臂都靠它。"""
    r = c.get(f"{app_url.rstrip('/')}/api/auth/sso/login")
    authorize_url = r.headers.get("location", "")
    r = c.get(authorize_url)
    if r.status_code != 200:
        raise AssertionError(f"取登录页失败: HTTP {r.status_code} "
                             f"{r.headers.get('location', '')}")
    r = c.post(parse_login_form(r.text),
               data={"username": username, "password": password,
                     "credentialId": ""})
    if r.status_code not in (302, 307):
        raise AssertionError(f"登录失败: HTTP {r.status_code} "
                             f"{parse_error_page(r.text)}")
    return {"authorize_url": authorize_url,
            "state": parse_qs(urlparse(authorize_url).query).get("state", [""])[0],
            "callback_url": r.headers.get("location", "")}


def run_negative_checks(args) -> dict:
    """负向验证：把改造前**实测成立**的两条攻击路径钉成回归。

    这两条都是真机对真 Keycloak 跑出来的，不是推演：
      C1 登录 CSRF —— 攻击者在自己浏览器走完登录、把回调 URL 甩给受害者，
         受害者浏览器（零 Cookie）打开后直接被登录成攻击者的账号。
      C2 同名账号提权 —— IdP 里建一个 preferred_username=admin 的用户，
         登录一次就拿到 KBase 超管（本地 bootstrap admin 的角色被直接继承）。
    """
    print("\n=== C. 负向验证（改造前实测成立的攻击路径必须被封住）===", flush=True)
    out: dict = {}
    base = args.app_url.rstrip("/")

    # ---- C1: 登录 CSRF ----
    with _client() as attacker:
        try:
            flow = _drive_login(attacker, base, args.username, args.password)
        except AssertionError as e:
            step(False, f"C1. 攻击者登录失败: {e}")
            return out
    with _client() as victim:          # 全新浏览器，与 KBase/IdP 零 Cookie 关系
        r = victim.get(base + "/api/auth/sso/callback"
                       f"?code={parse_qs(urlparse(flow['callback_url']).query).get('code', [''])[0]}"
                       f"&state={flow['state']}")
        me = victim.get(f"{base}/api/auth/me")
        ok = r.status_code == 400 and me.status_code == 401
        out["csrf_callback_status"] = r.status_code
        step(ok, f"C1. 登录 CSRF 已被挡：受害者打开别人的回调 URL → "
                f"HTTP {r.status_code}（要求 400），随后 /api/auth/me → "
                f"{me.status_code}（要求 401）")

    # ---- C2: 同名账号提权 ----
    if args.collision_username:
        with _client() as c:
            try:
                flow = _drive_login(c, base, args.collision_username,
                                    args.password)
            except AssertionError as e:
                step(False, f"C2. IdP 侧 {args.collision_username} 登录失败: {e}")
                return out
            r = c.get(flow["callback_url"])
            me = c.get(f"{base}/api/auth/me")
            ok = r.status_code == 403 and me.status_code == 401
            out["collision_callback_status"] = r.status_code
            step(ok, f"C2. 同名账号提权已被挡：IdP 用户 "
                    f"{args.collision_username!r} 登录 → HTTP {r.status_code}"
                    f"（要求 403），随后 /api/auth/me → {me.status_code}"
                    f"（要求 401，即没拿到任何身份）")
            if r.status_code == 200:
                print(f"    ⚠️ 正文: {r.text[:200]}", flush=True)
    return out


def run_kbase_flow(args) -> dict:
    print("\n=== A. 经过 KBase 的完整授权码流（真 Keycloak，无打桩）===", flush=True)
    out: dict = {}
    with _client() as c:
        base = args.app_url.rstrip("/")

        # 1) SSO 开关
        r = c.get(f"{base}/api/auth/sso/status")
        _log_exchange("GET /api/auth/sso/status", r, r.text[:120])
        step(r.status_code == 200 and r.json().get("enabled") is True,
             f"1. SSO 开关: {r.json() if r.status_code == 200 else r.text[:120]}")

        # 2) 302/307 跳转到真 IdP
        r = c.get(f"{base}/api/auth/sso/login")
        loc = r.headers.get("location", "")
        _log_exchange("GET /api/auth/sso/login", r, loc[:160])
        if not step(r.status_code in (302, 307) and loc.startswith(args.issuer),
                    f"2. 跳转 authorize（{r.status_code}）: {loc[:160]}"):
            return out
        q = parse_qs(urlparse(loc).query)
        state = q.get("state", [""])[0]
        out["authorize_url"] = loc
        out["state_from_redirect"] = state
        step(bool(state) and q.get("response_type") == ["code"]
             and q.get("scope", [""])[0] == "openid profile email",
             f"2b. authorize 参数: response_type={q.get('response_type')} "
             f"scope={q.get('scope')} client_id={q.get('client_id')} "
             f"redirect_uri={q.get('redirect_uri')}")
        # 测试 client 是按"强制 PKCE"配的，所以后面第 3 步能取到登录页本身
        # 就说明 code_challenge 是对的；这里再把参数显式钉一遍，失败时能一眼
        # 看出是 PKCE 缺了还是别的问题。
        step(q.get("code_challenge_method") == ["S256"]
             and len(q.get("code_challenge", [""])[0]) >= 43,
             f"2c. authorize 带 PKCE: code_challenge_method="
             f"{q.get('code_challenge_method')} "
             f"code_challenge 长度={len(q.get('code_challenge', [''])[0])}")

        # 3) 真登录页
        r = c.get(loc)
        _log_exchange("GET authorize (登录页)", r, f"{len(r.text)} bytes")
        if not step(r.status_code == 200 and "kc-form-login" in r.text,
                    f"3. 取到 Keycloak 登录页（{r.status_code}, {len(r.text)} bytes）"):
            return out
        action = parse_login_form(r.text)
        print(f"    login action = {action[:150]}", flush=True)

        # 4) POST 用户名/口令
        r = c.post(action, data={"username": args.username,
                                 "password": args.password,
                                 "credentialId": ""})
        loc2 = r.headers.get("location", "")
        _log_exchange("POST 登录表单", r, loc2[:160])
        if r.status_code not in (302, 307):
            # Keycloak 把表单校验失败重新渲染登录页（HTTP 200 或 400 都有），
            # 错误文案在 kc-error-message 里，必须打出来否则无从排查
            step(False, f"4. 登录未通过（HTTP {r.status_code}）: "
                        f"{parse_error_page(r.text)}")
            return out
        step(True, f"4. 登录成功，IdP 回调（{r.status_code}）")

        # 5) IdP 回调 KBase：KBase 侧换 token + userinfo + 落 Cookie
        r = c.get(loc2)
        _log_exchange("GET /api/auth/sso/callback", r, r.headers.get("location", ""))
        cb_q = parse_qs(urlparse(loc2).query)
        out["callback_has_code"] = bool(cb_q.get("code"))
        out["state_echoed_back"] = cb_q.get("state", [""])[0]
        if not step(r.status_code in (302, 307),
                    f"5. KBase 回调成功（{r.status_code} → {r.headers.get('location')}）"):
            print(f"    正文: {r.text[:400]}", flush=True)
            return out
        set_cookie = r.headers.get("set-cookie", "")
        out["set_cookie"] = set_cookie
        step("kbase_session=" in set_cookie,
             f"5b. 落了会话 Cookie: {set_cookie[:110]}")
        out["session_cookie"] = c.cookies.get("kbase_session", "")

        # 6) 会话生效：/api/auth/me
        r = c.get(f"{base}/api/auth/me")
        _log_exchange("GET /api/auth/me", r, r.text[:200])
        me = r.json() if r.status_code == 200 else {}
        out["me"] = me
        step(r.status_code == 200 and me.get("username") == args.username,
             f"6. 认证身份: HTTP {r.status_code} {me}")

        # 7) 真业务接口
        r = c.get(f"{base}/api/kb")
        _log_exchange("GET /api/kb", r, r.text[:200])
        step(r.status_code == 200,
             f"7. 已认证业务接口 GET /api/kb: HTTP {r.status_code} {r.text[:160]}")

        # 8) 会话 JWT 解出来看看（不校验签名，只看 KBase 落了什么）
        tok = out.get("session_cookie") or ""
        if tok.count(".") == 2:
            import base64
            def _b64(s):
                return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
            try:
                out["session_claims"] = _b64(tok.split(".")[1])
                step(True, f"8. 会话 JWT claims: {out['session_claims']}")
            except Exception as e:      # noqa: BLE001
                step(False, f"8. 会话 JWT 解析失败: {e}")

        # 9) 直接打 KBase API（不经 Cookie，用 Bearer）——顺带证明 Cookie 是
        #    唯一凭据来源时也成立
        r = c.get(f"{base}/api/auth/me", headers={"Cookie": ""})
        step(r.status_code == 401, f"9. 无 Cookie → 401（未鉴权不泄露）: {r.status_code}")
    return out


# --------------------------------------------------------------------------
# B) 直连探针：自己换 code，把 Keycloak 真实响应形状打出来
# --------------------------------------------------------------------------
def _redact_token_blob(obj: dict) -> dict:
    """把 token 响应里的易变字段换成稳定占位符，便于当夹具入库。

    session_state / 各 token 每次登录都不同，必须洗掉，否则夹具一入库就是
    "每次都变"的脏数据（回归测试会莫名其妙地 red）。"""
    keep = {"token_type", "expires_in", "refresh_expires_in", "scope",
            "not-before-policy"}
    out = {k: v for k, v in obj.items() if k in keep}
    out["access_token"] = "<ACCESS_TOKEN>"
    if "id_token" in obj:
        out["id_token"] = "<ID_TOKEN>"
    if "refresh_token" in obj:
        out["refresh_token"] = "<REFRESH_TOKEN>"
    if "session_state" in obj:
        out["session_state"] = "<SESSION_STATE>"
    return out


# id_token 里每次都会变 / 与本次令牌绑定的声明，夹具里一律洗掉
_VOLATILE_ID_CLAIMS = ("iat", "exp", "auth_time", "jti", "nonce",
                       "session_state", "sid", "at_hash")


def _redact_userinfo(obj: dict) -> dict:
    out = dict(obj)
    if "sub" in out:
        out["sub"] = "<SUB_UUID>"
    return out


def run_direct_probe(args, dump_fixture: str | None) -> dict:
    """不经 KBase：自己走一遍授权码流，拿 code → token → userinfo。

    目的有二：(1) 证明 KBase 之外确实存在一条可用的真链路，KBase 的 502 之类
    问题不会与 IdP 侧混淆；(2) 抓 Keycloak 返回的真实 JSON 形状当 CI 夹具。
    """
    print("\n=== B. 直连探针（不经 KBase）：抓 Keycloak 真实响应形状 ===", flush=True)
    out: dict = {}

    r = httpx.get(f"{args.issuer}/.well-known/openid-configuration", timeout=20.0)
    r.raise_for_status()
    meta = r.json()
    out["discovery"] = {
        "issuer": meta.get("issuer"),
        "authorization_endpoint": meta.get("authorization_endpoint"),
        "token_endpoint": meta.get("token_endpoint"),
        "userinfo_endpoint": meta.get("userinfo_endpoint"),
        "jwks_uri": meta.get("jwks_uri"),
        "code_challenge_methods_supported": meta.get("code_challenge_methods_supported"),
        "token_endpoint_auth_methods_supported":
            meta.get("token_endpoint_auth_methods_supported"),
    }
    print(json.dumps(out["discovery"], indent=2, ensure_ascii=False), flush=True)
    step(meta.get("issuer") == args.issuer,
         f"B1. discovery.issuer 与配置一致: {meta.get('issuer')}")

    redirect_uri = args.redirect_uri or (
        args.app_url.rstrip("/") + "/api/auth/sso/callback")
    with _client() as c:
        # 直连也要带 PKCE：测试 client 是按"强制 PKCE"配的（见 provision 脚本），
        # 不带就会被 Keycloak 回 error=invalid_request
        verifier, challenge = oidc_pkce_pair()
        params = {"response_type": "code", "client_id": args.client_id,
                  "redirect_uri": redirect_uri,
                  "scope": "openid profile email", "state": "direct-probe-state",
                  "nonce": "direct-probe-nonce",
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        auth_url = f"{meta['authorization_endpoint']}?{urlencode(params)}"
        r = c.get(auth_url)
        if not step(r.status_code == 200 and "kc-form-login" in r.text,
                    f"B2. 直连取登录页: HTTP {r.status_code}"):
            return out
        r = c.post(parse_login_form(r.text),
                   data={"username": args.username, "password": args.password,
                         "credentialId": ""})
        loc = r.headers.get("location", "")
        if not step(r.status_code in (302, 307),
                    f"B3. 直连登录: HTTP {r.status_code} → {loc[:120]}"):
            return out
        code = parse_qs(urlparse(loc).query).get("code", [""])[0]
        step(bool(code), f"B4. 拿到 authorization code（长度 {len(code)}）")

        r = httpx.post(meta["token_endpoint"], timeout=20.0, data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": args.client_id,
            "client_secret": args.client_secret,
            "code_verifier": verifier})
        out["token_status"] = r.status_code
        if not step(r.status_code == 200, f"B5. token 端点: HTTP {r.status_code}"
                                          f" {r.text[:200] if r.status_code != 200 else ''}"):
            return out
        tokens = r.json()
        out["token_response"] = _redact_token_blob(tokens)
        out["token_keys"] = sorted(tokens.keys())
        print("    token keys = " + ", ".join(out["token_keys"]), flush=True)

        r = httpx.get(meta["userinfo_endpoint"], timeout=20.0,
                      headers={"Authorization": f"Bearer {tokens['access_token']}"})
        out["userinfo_status"] = r.status_code
        if not step(r.status_code == 200, f"B6. userinfo 端点: HTTP {r.status_code}"):
            return out
        userinfo = r.json()
        out["userinfo"] = _redact_userinfo(userinfo)
        out["userinfo_keys"] = sorted(userinfo.keys())
        print("    userinfo = " + json.dumps(out["userinfo"], ensure_ascii=False),
              flush=True)
        # B7：这是整件事的关键断言——KBase 的 resolve_username 依赖
        # preferred_username/email 存在且非空
        step(userinfo.get("preferred_username") == args.username,
             f"B7. userinfo.preferred_username == 登录名: "
             f"{userinfo.get('preferred_username')!r}")
        step(bool(userinfo.get("sub")), f"B8. userinfo.sub 存在: {userinfo.get('sub')!r}")

        # B9: ID token 的 claims（KBase 当前完全没看它——记下来当已知缺口）
        if "id_token" in tokens:
            import base64
            payload = tokens["id_token"].split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(
                payload + "=" * (-len(payload) % 4)))
            out["id_token_claims"] = {k: ("<SUB_UUID>" if k == "sub" else v)
                                      for k, v in claims.items()
                                      if k not in _VOLATILE_ID_CLAIMS}
            print("    id_token claims = "
                  + json.dumps(out["id_token_claims"], ensure_ascii=False), flush=True)
            step(claims.get("nonce") == "direct-probe-nonce",
                 "B9. ID token 回带 nonce（KBase 当前不校验——见交付说明）")

    if dump_fixture:
        fixture = {
            "_source": "真 Keycloak 26.0 实拍（scripts/dev/verify_sso_real_idp.sh）",
            "issuer": args.issuer,
            "discovery_subset": out.get("discovery"),
            "token_response_keys": out.get("token_keys"),
            "token_response": out.get("token_response"),
            "userinfo": out.get("userinfo"),
            "id_token_claims": out.get("id_token_claims"),
        }
        os.makedirs(os.path.dirname(dump_fixture), exist_ok=True)
        with open(dump_fixture, "w", encoding="utf-8") as f:
            json.dump(fixture, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.write("\n")
        print(f"\n夹具已写出: {dump_fixture}", flush=True)
    return out


# --------------------------------------------------------------------------
# uvicorn 工厂（给 verify 脚本后台起真 KBase app 用）
# --------------------------------------------------------------------------
def create_probe_app():
    """uvicorn --factory 入口：真 create_app(auth="on")，只把向量器/LLM 换成假
    的——生产默认会加载 bge-m3 真模型（要下模型、吃几个 G 内存），而本验收只
    关心认证链路。鉴权、中间件、SSO 三端点、全部路由都是生产代码。"""
    cfg_path = os.environ["KBASE_SSO_VERIFY_CONFIG"]
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))

    class _FakeEmbedder:            # 与 tests/conftest.py 的 FakeEmbedder 同构
        dimension = 8

        def embed(self, texts):
            import hashlib
            out = []
            for t in texts:
                h = int(hashlib.md5(t.encode("utf-8")).hexdigest(), 16)
                out.append([((h >> (i * 4)) % 100) / 100.0 for i in range(8)])
            return out

    class _FakeLLM:
        model = "fake"

        async def stream(self, messages, **params):
            yield "ok"

        async def complete(self, messages, **params):
            return "ok"

    from kbase.api.main import create_app
    return create_app(config_path=cfg_path, embedder=_FakeEmbedder(),
                      llms={"fake": _FakeLLM()}, reranker=False, auth="on")


def main() -> int:
    ap = argparse.ArgumentParser(description="KBase 企业 SSO 真 IdP 授权码流探针")
    ap.add_argument("--app-url", default="http://127.0.0.1:8092",
                    help="KBase app 地址（redirect_uri 由它的 base_url 推出）")
    ap.add_argument("--issuer", required=True,
                    help="IdP issuer，如 http://127.0.0.1:8090/realms/kbase")
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", default="")
    ap.add_argument("--username", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--redirect-uri", default="",
                    help="直连探针用；默认按 app-url 推 /api/auth/sso/callback")
    ap.add_argument("--dump-fixture", default="")
    ap.add_argument("--direct-only", action="store_true")
    ap.add_argument("--collision-username", default="",
                    help="IdP 侧与 KBase 本地账号同名的用户（默认 admin），"
                         "用来验证同名账号提权被拒；留空跳过 C2")
    args = ap.parse_args()

    t0 = time.time()
    if not args.direct_only:
        run_kbase_flow(args)
        run_negative_checks(args)
    run_direct_probe(args, args.dump_fixture or None)

    failed = [m for ok, m in _results if not ok]
    print(f"\n=== 结论：{len(_results) - len(failed)}/{len(_results)} 项通过，"
          f"耗时 {time.time() - t0:.1f}s ===", flush=True)
    for m in failed:
        print(f"  ✗ {m}", flush=True)
    if failed:
        print("（注意：单条 FAIL 不必然是产品 Bug——也可能是探针断言写错或 Keycloak "
              "版本差异，须逐条看现象）", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
