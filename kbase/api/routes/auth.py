"""认证路由：登录/登出/当前身份（spec §2/§7）+ 企业 SSO（M6-8 OIDC）。

login 与 SSO 三端点挂在 app 级而不是共享 router 上——它们必须绕开 router
级的 actor 依赖（未登录者才需要登录），是全部 /api 路由中仅有的例外。"""
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi import Response as FastAPIResponse
from fastapi.responses import RedirectResponse

from kbase.api.routes import RouteDeps
from kbase.api.schemas import LoginBody
from kbase.api.services import Services
from kbase.audit import write_audit
from kbase.auth import oidc, security
from kbase.models import User


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
            raise HTTPException(401, "用户名或密码错误，或账号已被禁用")
        token = security.create_session_token(user.username, user.role, secret=secret)
        response.set_cookie(
            "kbase_session", token, httponly=True, samesite="lax",
            max_age=security.SESSION_TOKEN_TTL_SECONDS)
        write_audit(sf, actor=user.username, action="login_success", ip=ip)
        return {"username": user.username, "role": user.role}

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
            raise HTTPException(404, "SSO 未启用")
        state = oidc.make_state(secret)
        return RedirectResponse(
            oidc.build_authorize_url(sso, _sso_redirect_uri(request), state))

    @app.get("/api/auth/sso/callback")
    def sso_callback(request: Request, code: str = "", state: str = ""):
        if not sso.enabled:
            raise HTTPException(404, "SSO 未启用")
        if not code or not oidc.verify_state(state, secret):
            raise HTTPException(400, "SSO 回调参数无效（state 校验失败或缺 code）")
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
            raise HTTPException(502, "IdP userinfo 缺少可用的用户名字段")
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
                raise HTTPException(401, "账号已被禁用")
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
        return {"username": actor["name"], "role": actor["role"]}
