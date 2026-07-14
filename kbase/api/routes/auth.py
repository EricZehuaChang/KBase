"""认证路由：登录/登出/当前身份（spec §2/§7）。

login 挂在 app 级而不是共享 router 上——它必须绕开 router 级的 actor 依赖
（未登录者才需要登录），是全部 /api 路由中唯一的例外。"""
from fastapi import FastAPI, HTTPException, Request
from fastapi import Response as FastAPIResponse

from kbase.api.routes import RouteDeps
from kbase.api.schemas import LoginBody
from kbase.api.services import Services
from kbase.audit import write_audit
from kbase.auth import security
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

    @router.post("/auth/logout", dependencies=[deps.require_viewer])
    def logout(response: FastAPIResponse):
        response.delete_cookie("kbase_session")
        return {"ok": True}

    @router.get("/auth/me", dependencies=[deps.require_viewer])
    def auth_me(request: Request):
        actor = request.state.actor
        return {"username": actor["name"], "role": actor["role"]}
