"""认证路由：登录/登出/当前身份（spec §2/§7）+ 企业 SSO（M6-8 OIDC）。

login、SSO 三端点与忘记密码两端点挂在 app 级而不是共享 router 上——它们
必须绕开 router 级的 actor 依赖（未登录者才需要用），是 /api 路由中仅有的
例外。"""
import json
import logging
import secrets
import time
import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi import Response as FastAPIResponse
from fastapi.responses import RedirectResponse

from kbase import email_templates, mailer, ratelimit
from kbase.api.routes import RouteDeps
from kbase.api.schemas import (ChangePasswordBody, ForgotBody, LanguageBody,
                               LoginBody, ProfileBody, ResetPasswordBody)
from kbase.api.services import Services
from kbase.audit import write_audit
from kbase.auth import oidc, security
from kbase.errors import AppError
from kbase.i18n_store import SUPPORTED_LANGUAGES
from kbase.models import AppSetting, User

logger = logging.getLogger(__name__)

# 重置 token 有效期（秒）。KV key 形如 pwreset:{token}，value 为
# json{"username", "exp"}——复用 AppSetting 存储，量级极小（同时在途的
# 重置请求个位数），不值得建表。
RESET_TOKEN_TTL_SECONDS = 30 * 60

# 登录失败的错误码与文案（T11：被闸拦下的 429 正文必须与它逐字节一致，见
# register() 里的 _login_locked）。
_INVALID_CREDENTIALS_CODE = "error.invalid_credentials"
_INVALID_CREDENTIALS_MESSAGE = "用户名或密码错误，或账号已被禁用"


def _invalid_credentials() -> AppError:
    """登录失败（用户名不存在 / 密码错 / 账号禁用）的统一响应：三种原因同一
    句话、同一状态码，响应里不作任何区分（防账号枚举）。"""
    return AppError(_INVALID_CREDENTIALS_CODE, _INVALID_CREDENTIALS_MESSAGE,
                    status=401)


def register(app: FastAPI, router, svc: Services, deps: RouteDeps, *,
             secret: str) -> None:
    sf = svc.sf
    # T11：登录/忘记密码/重置密码的失败锁定参数（阈值、窗口、退避基数与上限，
    # 见 kbase/config.py 的 LoginGuardConfig）。计数源是审计表里的 login_failed
    # 等留痕行，判定逻辑在 kbase/ratelimit.py 的 LoginGuard。
    guard_cfg = svc.cfg.login_guard

    def _audit_lock(actor: str, endpoint: str, verdict: dict, ip) -> None:
        """给一次被闸拦下的请求落审计行（action="login_locked"）。

        只记这一行、不记 login_failed：请求没被验证过，写成"登录失败"是假账。
        但这一行本身会计入**退避档位**（阈值计数不算它）——反复硬打只会等得
        更久，直到封顶，见 kbase/ratelimit.py 的两层判定说明。detail 里带上
        两个维度的计数，运维侧据此区分"这个账号在被爆"与"这个 IP 在扫"。
        """
        write_audit(sf, actor=actor, action=ratelimit.LOGIN_LOCKED, ip=ip,
                    detail={"endpoint": endpoint,
                            "retry_after": verdict["retry_after"],
                            "username_attempts": verdict["username_attempts"],
                            "ip_attempts": verdict["ip_attempts"],
                            "username_refusals": verdict["username_refusals"],
                            "ip_refusals": verdict["ip_refusals"]})

    def _login_locked(retry_after: int) -> HTTPException:
        """登录被闸拦下时的 429 响应。

        **正文与 401 的 error.invalid_credentials 逐字节一致**（同一 code /
        params / message，同一 JSON 编码），只有状态码与 Retry-After 头不同：
        锁定是按"请求里的用户名字符串"与来源 IP 判定的（账号存在与否一律同样
        计数、同样锁定），响应里若出现任何差异就等于告诉攻击者这个账号存在
        （防账号枚举）。前端按 detail.code 走 i18n，用户看到的仍是"用户名或
        密码错误，或账号已被禁用"，与普通输错密码无区别。
        """
        return HTTPException(
            status_code=429,
            detail={"code": _INVALID_CREDENTIALS_CODE, "params": {},
                    "message": _INVALID_CREDENTIALS_MESSAGE},
            headers={"Retry-After": str(retry_after)})

    def _too_many_requests(retry_after: int) -> HTTPException:
        """忘记密码 / 重置密码被闸拦下时的 429。

        这两处的闸只看来源 IP、不看账号，429 本身不泄露任何账号信息，所以给
        明确文案即可，不必像登录那样与 401 同文案。
        """
        return HTTPException(
            status_code=429,
            detail={"code": "error.too_many_requests",
                    "params": {"retry_after": retry_after},
                    "message": f"请求过于频繁，请在 {retry_after} 秒后重试"},
            headers={"Retry-After": str(retry_after)})

    @app.post("/api/auth/login")
    def login(body: LoginBody, response: FastAPIResponse, request: Request):
        client = request.client
        ip = client.host if client is not None else None
        # T11：先过登录闸，再查库/验密码——窗口内同一用户名或同一 IP 的
        # login_failed 达阈值就直接 429（不查库也不做 bcrypt：爆破连"账号存不
        # 存在"的时间差都拿不到，也省掉 CPU）。
        verdict = ratelimit.login_guard.check(
            sf, guard_cfg, actions=ratelimit.LOGIN_ACTIONS,
            username=body.username, ip=ip)
        if verdict["retry_after"]:
            _audit_lock(body.username, "login", verdict, ip)
            raise _login_locked(verdict["retry_after"])
        with sf() as s:
            user = s.query(User).filter_by(username=body.username).first()
        if (user is None or user.disabled
                or not security.verify_password(body.password, user.password_hash)):
            # 这一行的 action 就是登录闸的计数源，必须与 ratelimit.LOGIN_FAILED
            # 同值（两边共用常量，不写字面量）。
            write_audit(sf, actor=body.username,
                        action=ratelimit.LOGIN_FAILED, ip=ip)
            raise _invalid_credentials()
        token = security.create_session_token(user.username, user.role, secret=secret)
        # 默认会话级 Cookie（不设 max_age/expires）：关浏览器即清除，重开必须
        # 重新登录；开着期间由 JWT 的 30 天有效期兜底（见 security 模块注释）。
        # 勾选"记住登录"时改持久 Cookie，时长与 JWT 有效期对齐——Cookie 活着
        # 而 JWT 过期只会得到 401 循环，两者必须同步。
        if body.remember:
            response.set_cookie(
                "kbase_session", token, httponly=True, samesite="lax",
                max_age=security.SESSION_TOKEN_TTL_SECONDS)
        else:
            response.set_cookie(
                "kbase_session", token, httponly=True, samesite="lax")
        write_audit(sf, actor=user.username, action="login_success", ip=ip)
        return {"username": user.username, "role": user.role}

    # ---- 忘记密码（邮箱重置，app 级：未登录者使用） ----

    @app.post("/api/auth/forgot")
    def forgot_password(body: ForgotBody, request: Request,
                        bg: BackgroundTasks):
        """按用户名或邮箱找账号，发重置链接邮件。无论命中与否都返回同一
        句话（防账号枚举）；发信走后台任务（响应时长不泄露命中与否）。

        T11：本端点未鉴权且会发信，按来源 IP 过闸（只看 IP、不看账号，所以
        429 本身不泄露账号是否存在）——计数源见 kbase/ratelimit.py 的
        FORGOT_ACTIONS。"""
        account = body.account.strip()
        client = request.client
        ip = client.host if client is not None else None
        verdict = ratelimit.login_guard.check(
            sf, guard_cfg, actions=ratelimit.FORGOT_ACTIONS, ip=ip)
        if verdict["retry_after"]:
            _audit_lock(account, "forgot", verdict, ip)
            raise _too_many_requests(verdict["retry_after"])
        with sf() as s:
            user = s.query(User).filter(
                (User.username == account) | (User.email == account)).first()
            # 顺手清掉过期 token，避免 KV 里越积越多
            now = time.time()
            for row in s.query(AppSetting).filter(
                    AppSetting.key.like("pwreset:%")).all():
                try:
                    if json.loads(row.value).get("exp", 0) < now:
                        s.delete(row)
                except (ValueError, TypeError):
                    s.delete(row)
            if user is not None and user.email and not user.disabled:
                token = secrets.token_urlsafe(32)
                s.add(AppSetting(key=f"pwreset:{token}", value=json.dumps(
                    {"username": user.username,
                     "exp": now + RESET_TOKEN_TTL_SECONDS})))
                login_url = str(request.base_url).rstrip("/")
                to_addr = user.email
                user_name = user.username
                mail_lang = mailer.email_language(sf, user.language)

                def _send():
                    try:
                        # 链接必须指 /login（守卫豁免路径，query 原样保留）——
                        # 指根路径 "/" 会被未登录守卫重定向成 /login?redirect=...，
                        # reset_token 被裹进 redirect 参数，重置表单读不到（真机
                        # 踩中：用户点开只见普通登录页）。
                        subject, text, html_body = email_templates.password_reset(
                            user_name, f"{login_url}/login?reset_token={token}",
                            lang=mail_lang)
                        mailer.send_mail(sf, to_addr, subject, text,
                                         html=html_body)
                    except Exception:
                        logger.exception("密码重置邮件发送失败: %s", to_addr)

                bg.add_task(_send)
            s.commit()
        write_audit(sf, actor=account, action=ratelimit.PASSWORD_FORGOT, ip=ip)
        return {"ok": True,
                "message": "如果该账号存在且已绑定邮箱，重置邮件已发出，请查收（含垃圾箱）"}

    @app.post("/api/auth/reset")
    def reset_password(body: ResetPasswordBody, request: Request):
        """凭邮件里的一次性 token 设新密码：验存在+未过期→落新哈希→销毁
        token（一次性）。

        T11：同 forgot，按来源 IP 过闸——token 是 32 字节随机串，爆破不现实，
        但本端点未鉴权且每次尝试都要查库，闸拦的是高频扫（计数源见
        kbase/ratelimit.py 的 RESET_ACTIONS）。"""
        client = request.client
        ip = client.host if client is not None else None
        verdict = ratelimit.login_guard.check(
            sf, guard_cfg, actions=ratelimit.RESET_ACTIONS, ip=ip)
        if verdict["retry_after"]:
            _audit_lock("unknown", "reset", verdict, ip)
            raise _too_many_requests(verdict["retry_after"])
        with sf() as s:
            row = s.get(AppSetting, f"pwreset:{body.token}")
            data = None
            if row is not None:
                try:
                    data = json.loads(row.value)
                except (ValueError, TypeError):
                    data = None
            if (data is None or data.get("exp", 0) < time.time()):
                if row is not None:
                    s.delete(row)
                    s.commit()
                write_audit(sf, actor="unknown",
                            action=ratelimit.PASSWORD_RESET_FAILED,
                            detail="invalid_or_expired_token", ip=ip)
                raise AppError("error.reset_invalid",
                               "重置链接无效或已过期，请重新发起忘记密码", status=400)
            user = s.query(User).filter_by(username=data["username"]).first()
            if user is None or user.disabled:
                s.delete(row)
                s.commit()
                raise AppError("error.account_not_found", "账号不存在或已被禁用", status=400)
            user.password_hash = security.hash_password(body.new_password)
            s.delete(row)
            s.commit()
            username = user.username
        write_audit(sf, actor=username, action="password_reset", ip=ip)
        return {"ok": True}

    # ---- 企业 SSO（M6-8 OIDC 授权码流）----
    # 三端点全部 app 级（未登录者使用）。sso.enabled=false 时 status 返回
    # 关闭、login/callback 404——不配置就完全不暴露攻击面。
    sso = svc.cfg.sso

    @app.get("/api/auth/sso/status")
    def sso_status():
        """登录页据此决定是否显示"企业账号登录"按钮。"""
        return {"enabled": sso.enabled}

    def _sso_redirect_uri(request: Request) -> str:
        # 以实际访问的 host 拼回调地址（支持反代/域名部署），路径固定
        return str(request.base_url).rstrip("/") + "/api/auth/sso/callback"

    @app.get("/api/auth/sso/login")
    def sso_login(request: Request):
        if not sso.enabled:
            raise AppError("error.sso_disabled", "SSO 未启用", status=404)
        state = oidc.make_state(secret)
        return RedirectResponse(
            oidc.build_authorize_url(sso, _sso_redirect_uri(request), state))

    @app.get("/api/auth/sso/callback")
    def sso_callback(request: Request, code: str = "", state: str = ""):
        if not sso.enabled:
            raise AppError("error.sso_disabled", "SSO 未启用", status=404)
        if not code or not oidc.verify_state(state, secret):
            raise AppError("error.sso_invalid_callback",
                           "SSO 回调参数无效（state 校验失败或缺 code）", status=400)
        client = request.client
        ip = client.host if client is not None else None
        try:
            userinfo = oidc.exchange_code(sso, code, _sso_redirect_uri(request))
        except Exception as e:      # IdP 网络/配置错误：给运维可读信息
            # T11 说明：这里与下面的禁用账号都记同一个 action（登录失败），
            # 于是也会计入该来源 IP 的登录失败计数——SSO 通道出问题不该成为
            # 绕开登录闸的旁路（state 是签名过的，外部无法凭空刷这两行）。
            write_audit(sf, actor="sso", action=ratelimit.LOGIN_FAILED,
                        detail=f"oidc_exchange: {e}"[:200], ip=ip)
            raise HTTPException(502, f"SSO 换取用户信息失败: {e}") from e
        username = oidc.resolve_username(userinfo)
        if not username:
            raise AppError("error.sso_no_username", "IdP userinfo 缺少可用的用户名字段", status=502)
        with sf() as s:
            user = s.query(User).filter_by(username=username).first()
            if user is None:
                # 首次 SSO 登录自动建号：默认角色，密码置随机（只能走 SSO 登录，
                # 角色细化仍在 KBase 用户管理页调整——单一权限事实源）
                user = User(id=str(uuid.uuid4()), username=username,
                            password_hash=security.hash_password(uuid.uuid4().hex),
                            role=sso.default_role)
                s.add(user)
                s.commit()
            if user.disabled:
                write_audit(sf, actor=username, action=ratelimit.LOGIN_FAILED,
                            detail="sso_disabled_user", ip=ip)
                raise AppError("error.account_disabled", "账号已被禁用", status=401)
            token = security.create_session_token(user.username, user.role,
                                                  secret=secret)
        write_audit(sf, actor=username, action="login_success",
                    detail="sso", ip=ip)
        resp = RedirectResponse("/")
        # 与密码登录同一会话策略：会话级 Cookie（关浏览器失效），JWT 30 天兜底
        resp.set_cookie("kbase_session", token, httponly=True, samesite="lax")
        return resp

    @router.post("/auth/logout", dependencies=[deps.require_viewer])
    def logout(response: FastAPIResponse):
        response.delete_cookie("kbase_session")
        return {"ok": True}

    @router.get("/auth/me", dependencies=[deps.require_viewer])
    def auth_me(request: Request):
        actor = request.state.actor
        # email：前端首登据此引导补录（用于忘记密码重置）。API Key 身份
        # 无对应用户行，email 为 None。
        with sf() as s:
            user = s.query(User).filter_by(username=actor["name"]).first()
            email = user.email if user else None
            # 账号级语言偏好（P2-4）：前端登录后据此覆盖本地检测。未设置
            # （None）或 API Key 身份 → null，前端跟随 localStorage/浏览器。
            language = user.language if user else None
            # 高级界面：editor 及以上（含 superadmin）恒开；viewer 看个人
            # 开关（管理员在用户管理里配置）。API Key 身份无用户行，按角色默认。
            advanced = (actor["role"] in ("superadmin", "admin", "editor")
                        or bool(user.advanced_ui if user else False))
            # 产品导览入口白名单（演示者专属）：超管恒可见；其余账号看
            # AppSetting KV `tour_allowed_users`（JSON 用户名数组）——按账号
            # 而非角色控制，普通 admin 同事不会误入导览。
            tour_enabled = actor["role"] == "superadmin"
            if not tour_enabled:
                row = s.get(AppSetting, "tour_allowed_users")
                if row is not None:
                    try:
                        tour_enabled = actor["name"] in json.loads(row.value)
                    except ValueError:
                        pass    # 脏 JSON 当作无白名单
        return {"username": actor["name"], "role": actor["role"],
                "email": email, "advanced_ui": advanced, "language": language,
                "tour_enabled": tour_enabled}

    @router.put("/auth/profile",
                dependencies=[deps.require_viewer, deps.audit_mutation])
    def update_profile(body: ProfileBody, request: Request):
        """登录用户维护自己的邮箱（首登引导填写，用于忘记密码重置）。"""
        actor = request.state.actor
        with sf() as s:
            user = s.query(User).filter_by(username=actor["name"]).first()
            if user is None:
                raise AppError("error.no_profile", "当前身份不支持维护资料", status=403)
            user.email = body.email.strip()
            s.commit()
        return {"ok": True}

    @router.put("/auth/language", dependencies=[deps.require_viewer])
    def update_language(body: LanguageBody, request: Request):
        """账号级界面语言偏好（P2-4）：登录用户手动切语言时回写本列，实现跨
        设备一致母语。白名单校验挡非法码；API Key 身份无用户行（403）。不挂
        审计——纯个人 UI 偏好，高频写入不进审计流。"""
        lang = body.language.strip()
        if lang not in SUPPORTED_LANGUAGES:
            raise AppError("error.unsupported_language", "不支持的语言：{lang}",
                           status=422, lang=lang)
        actor = request.state.actor
        with sf() as s:
            user = s.query(User).filter_by(username=actor["name"]).first()
            if user is None:
                raise AppError("error.no_profile", "当前身份不支持维护资料", status=403)
            user.language = lang
            s.commit()
        return {"ok": True}

    @router.post("/auth/change-password",
                 dependencies=[deps.require_viewer, deps.audit_mutation])
    def change_password(body: ChangePasswordBody, request: Request):
        """登录用户自助改密（此前只能 admin 代改）：旧密码复核后落新哈希。
        仅账号会话可用——API Key 身份没有对应"本人密码"语义（403）。"""
        actor = request.state.actor
        with sf() as s:
            user = s.query(User).filter_by(username=actor["name"]).first()
            if user is None:
                raise AppError("error.no_change_pw",
                               "当前身份不支持修改密码（API Key 无账号密码）", status=403)
            if not security.verify_password(body.old_password, user.password_hash):
                raise AppError("error.old_password_wrong", "旧密码不正确", status=401)
            user.password_hash = security.hash_password(body.new_password)
            s.commit()
        return {"ok": True}
