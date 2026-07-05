"""FastAPI 鉴权依赖：get_current_actor（Cookie JWT / Bearer API Key 双通道）、
require_role（角色序校验）、Origin 同源中间件（CSRF 防护）。

actor 统一表示为 {"name": str, "role": str}：Cookie 通道 name=用户名，
Bearer 通道 name=API Key 的 name（供审计落 actor 字段，G3 用）。
"""
from fastapi import Depends, HTTPException, Request

from kbase.auth import security
from kbase.models import ApiKey, User

SESSION_COOKIE_NAME = "kbase_session"
API_KEY_HEADER_PREFIX = "Bearer "

# 角色序：admin > editor > viewer。数值越大权限越高。
_ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401, detail="未认证：请提供有效的会话 Cookie 或 API Key",
        headers={"WWW-Authenticate": "Bearer"})


def make_get_current_actor(sf, secret: str):
    """返回一个可用作 FastAPI Depends 的函数，绑定给定的 session factory 与
    JWT secret（生产路径下由 create_app 在应用启动时解析一次并闭包捕获）。"""

    def get_current_actor(request: Request) -> dict:
        cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
        if cookie_token:
            try:
                payload = security.decode_session_token(cookie_token, secret=secret)
            except security.InvalidTokenError:
                raise _unauthorized()
            username = payload.get("sub")
            with sf() as s:
                user = s.query(User).filter_by(username=username).first()
            if user is None or user.disabled:
                raise _unauthorized()
            return {"name": user.username, "role": user.role}

        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith(API_KEY_HEADER_PREFIX):
            full_key = auth_header[len(API_KEY_HEADER_PREFIX):]
            key_hash = security.hash_api_key(full_key)
            with sf() as s:
                row = s.query(ApiKey).filter_by(key_hash=key_hash).first()
            if row is None or row.revoked:
                raise _unauthorized()
            return {"name": row.name, "role": row.role}

        raise _unauthorized()

    return get_current_actor


def require_role(get_current_actor, min_role: str):
    """工厂：返回一个依赖，要求 actor 角色 >= min_role（admin>editor>viewer）。
    403 detail 用中文，前端可直接展示。"""
    min_rank = _ROLE_RANK[min_role]

    def _check(actor: dict = Depends(get_current_actor)) -> dict:
        if _ROLE_RANK[actor["role"]] < min_rank:
            raise HTTPException(status_code=403, detail="权限不足：当前角色无法执行此操作")
        return actor

    return _check


def make_origin_guard_middleware():
    """返回一个 Starlette HTTP 中间件函数：非 GET 请求若带 Origin 头，
    校验其 host 与请求 Host 是否同源，不同源则 403（CSRF 防护）。
    无 Origin 头的请求（非浏览器客户端，如脚本/MCP 走 Bearer）直接放行——
    它们不依赖 Cookie，不存在 CSRF 风险。"""
    from urllib.parse import urlparse

    async def origin_guard(request: Request, call_next):
        if request.method != "GET":
            origin = request.headers.get("origin")
            if origin:
                origin_host = urlparse(origin).netloc
                if origin_host != request.url.netloc:
                    return _forbidden_response()
        return await call_next(request)

    return origin_guard


def _forbidden_response():
    from starlette.responses import JSONResponse
    return JSONResponse(status_code=403, content={"detail": "跨站请求被拒绝：来源不受信任"})
