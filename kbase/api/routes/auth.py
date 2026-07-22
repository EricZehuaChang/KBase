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

from kbase import email_templates, mailer
from kbase.api.routes import RouteDeps
from kbase.api.schemas import (ChangePasswordBody, ForgotBody, LoginBody,
                               ProfileBody, ResetPasswordBody)
from kbase.api.services import Services
from kbase.audit import write_audit
from kbase.auth import oidc, security
from kbase.errors import AppError
from kbase.models import AppSetting, User

logger = logging.getLogger(__name__)

# 重置 token 有效期（秒）。KV key 形如 pwreset:{token}，value 为
# json{"username", "exp"}——复用 AppSetting 存储，量级极小（同时在途的
# 重置请求个位数），不值得建表。
RESET_TOKEN_TTL_SECONDS = 30 * 60


def register(app: FastAPI, router, svc: Services, deps: RouteDeps, *,
             secret: str) -> None:
    sf = svc.sf

    @app.post("/api/auth/login")
    def login(body: LoginBody, response: FastAPIResponse, request: Request):
        with sf() as s:
            user = s.query(User).filter_by(username=body.username).first()
        client = request.client
        ip = client.host if client is not None else None
        if (user is None or user.disabled
                or not security.verify_password(body.password, user.password_hash)):
            write_audit(sf, actor=body.username, action="login_failed", ip=ip)
            raise AppError("error.invalid_credentials",
                           "用户名或密码错误，或账号已被禁用", status=401)
        token = security.create_session_token(user.username, user.role, secret=secret)
        response.set_cookie(
            "kbase_session", token, httponly=True, samesite="lax",
            max_age=security.SESSION_TOKEN_TTL_SECONDS)
        write_audit(sf, actor=user.username, action="login_success", ip=ip)
        return {"username": user.username, "role": user.role}

    # ---- 忘记密码（邮箱重置，app 级：未登录者使用） ----

    @app.post("/api/auth/forgot")
    def forgot_password(body: ForgotBody, request: Request,
                        bg: BackgroundTasks):
        """按用户名或邮箱找账号，发重置链接邮件。无论命中与否都返回同一
        句话（防账号枚举）；发信走后台任务（响应时长不泄露命中与否）。"""
        account = body.account.strip()
        client = request.client
        ip = client.host if client is not None else None
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

                def _send():
                    try:
                        subject, text, html_body = email_templates.password_reset(
                            user_name, f"{login_url}/?reset_token={token}")
                        mailer.send_mail(sf, to_addr, subject, text,
                                         html=html_body)
                    except Exception:
                        logger.exception("密码重置邮件发送失败: %s", to_addr)

                bg.add_task(_send)
            s.commit()
        write_audit(sf, actor=account, action="password_forgot", ip=ip)
        return {"ok": True,
                "message": "如果该账号存在且已绑定邮箱，重置邮件已发出，请查收（含垃圾箱）"}

    @app.post("/api/auth/reset")
    def reset_password(body: ResetPasswordBody, request: Request):
        """凭邮件里的一次性 token 设新密码：验存在+未过期→落新哈希→销毁
        token（一次性）。"""
        client = request.client
        ip = client.host if client is not None else None
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
                write_audit(sf, actor="unknown", action="password_reset_failed",
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
            write_audit(sf, actor="sso", action="login_failed",
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
                write_audit(sf, actor=username, action="login_failed",
                            detail="sso_disabled_user", ip=ip)
                raise AppError("error.account_disabled", "账号已被禁用", status=401)
            token = security.create_session_token(user.username, user.role,
                                                  secret=secret)
        write_audit(sf, actor=username, action="login_success",
                    detail="sso", ip=ip)
        resp = RedirectResponse("/")
        resp.set_cookie("kbase_session", token, httponly=True, samesite="lax",
                        max_age=security.SESSION_TOKEN_TTL_SECONDS)
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
            # 高级界面：editor/admin 恒开；viewer 看个人开关（管理员在用户
            # 管理里配置）。API Key 身份无用户行，按角色默认。
            advanced = (actor["role"] in ("admin", "editor")
                        or bool(user.advanced_ui if user else False))
        return {"username": actor["name"], "role": actor["role"],
                "email": email, "advanced_ui": advanced}

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
