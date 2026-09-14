"""FastAPI 鉴权依赖：get_current_actor（Cookie JWT / Bearer API Key 双通道）、
require_role（角色序校验）、Origin 同源中间件（CSRF 防护）。

actor 统一表示为 {"name": str, "role": str}：Cookie 通道 name=用户名，
Bearer 通道 name=API Key 的 name（供审计落 actor 字段，G3 用）。

T09 给 Bearer 通道补的字段（Cookie 通道没有它们，故一律用 .get 读）：
key_id（Key 行 id，限流/用量按它归档）、scope_kb_ids（库级白名单）、
rpm/daily_quota（配额，仅 kbase/ratelimit.py 的限流依赖读取，不参与授权）。

G3 角色矩阵：get_current_actor 解析出 actor 后会把它写进
request.state.actor（副作用），require_role(min_role) 不再自己发起鉴权，
而是读 request.state.actor——这样它可以在路由级按需 Depends，且能配合
auth="off" 模式下的 synthetic_admin_actor 依赖（见 make_synthetic_admin_actor_dependency）
一起工作：off 模式下不校验凭据，直接把 request.state.actor 设成一个
rank 最高的合成 admin，令所有 require_role 检查天然放行（角色矩阵在
off 模式下是无操作，行为与鉴权改造前一致）。
"""
import json
from datetime import datetime

from fastapi import HTTPException, Request

from kbase import ratelimit
from kbase.auth import security
from kbase.models import ApiKey, User
from kbase.ratelimit import ip_allowed

SESSION_COOKIE_NAME = "kbase_session"
API_KEY_HEADER_PREFIX = "Bearer "

# off 模式下审计要落的 actor 名——不是真实用户，只是标注"鉴权关闭"。
ANONYMOUS_ACTOR_NAME = "anonymous"

# 角色序：superadmin > admin > editor > viewer。数值越大权限越高。
# superadmin（超级管理员）在管理体系之外：普通 admin 管不到它（用户管理里
# 不能建/改/禁超管账号，见 routes/admin.py），它则拥有一切权限——rank 序
# 保证所有 require_role 检查对超管天然放行，无需新增依赖。
_ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2, "superadmin": 3}


def role_rank(role: str) -> int:
    """角色 → 权限序（未知角色按最低权 0 处理，不抛——防御脏数据）。
    供业务代码做"操作者是否够级"判断（如仅超管可管超管账号）。"""
    return _ROLE_RANK.get(role, 0)


def client_ip(request: Request) -> str | None:
    """请求来源 IP = 传输层对端地址（request.client.host）。

    与 routes/auth.py 登录处理器**逐字同算法**（审计的 ip 字段同一口径）：
    **不信任 X-Forwarded-For / X-Real-IP**——没有可信反代配置时任何客户端
    都能自带这两个头，用它做 API Key 的 IP 白名单等于没做。反代部署要拿到
    真实客户端 IP，正确做法是让反代改写对端地址（或在引入可信代理配置后
    统一在这里解析）——当前仓库无该配置，故一律用对端地址。
    """
    client = request.client
    return client.host if client is not None else None


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401, detail="未认证：请提供有效的会话 Cookie 或 API Key",
        headers={"WWW-Authenticate": "Bearer"})


def _api_key_expired() -> HTTPException:
    """API Key 过期：仍是 401（凭据不可用），但带 code=api_key_expired——
    集成方/前端据此区分"该换 Key 了"与"Key 根本不对"。detail 用
    {code,message} 结构化对象，与 AppError 的响应形状一致（核心逻辑见
    kbase/errors.py，前端 core.ts 的 detail.code 分支直接消费）。"""
    return HTTPException(
        status_code=401,
        detail={"code": "api_key_expired", "message": "API Key 已过期"},
        headers={"WWW-Authenticate": "Bearer"})


def _ip_not_allowed() -> HTTPException:
    """来源 IP 不在该 Key 的白名单内：403（身份有效、位置不对）——
    与"凭据无效"的 401 分开，便于集成方定位是换网络还是换 Key。"""
    return HTTPException(
        status_code=403,
        detail={"code": "ip_not_allowed",
                "message": "来源 IP 不在该 API Key 的 IP 白名单内"})


def make_get_current_actor(sf, secret: str):
    """返回一个可用作 FastAPI Depends 的函数，绑定给定的 session factory 与
    JWT secret（生产路径下由 create_app 在应用启动时解析一次并闭包捕获）。

    解析出的 actor 会先写入 request.state.actor 再返回——下游的
    require_role 依赖（及 G3 审计钩子）读这个 state，不重复解析鉴权。"""

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
            # user_id：M5-1 F2 会话归属过滤用（kbase/conversations.py）。Cookie
            # 通道背后是真实 users 表行，有稳定 id，可以把新建的会话记到这个人
            # 名下。
            actor = {"name": user.username, "role": user.role, "user_id": user.id}
            request.state.actor = actor
            return actor

        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith(API_KEY_HEADER_PREFIX):
            full_key = auth_header[len(API_KEY_HEADER_PREFIX):]
            key_hash = security.hash_api_key(full_key)
            ip = client_ip(request)
            with sf() as s:
                row = s.query(ApiKey).filter_by(key_hash=key_hash).first()
                # T09 校验顺序（先身份后来源，且每一步都给得出最具体的判定）：
                # 1) 吊销或停用 → 401。revoked 不可恢复（DELETE 端点只置位）；
                #    disabled 可恢复（PATCH 端点）——两者对外表现一致（不区分，
                #    免得泄漏"这把 Key 只是被临时停用，等会儿还能用"）。
                #    老库补列 disabled=NULL → falsy → 未停用（见 migrations.py）。
                if row is None or row.revoked or row.disabled:
                    raise _unauthorized()
                # 2) 过期 → 401 + code=api_key_expired。NULL=永不过期。
                #    expires_at 是 naive UTC，与写入口径一致（见 admin.py）。
                if (row.expires_at is not None
                        and row.expires_at <= datetime.utcnow()):
                    raise _api_key_expired()
                # 3) 来源 IP 不在白名单 → 403 + code=ip_not_allowed。
                #    ip_allow=NULL=不限来源（老库/未配置，行为与升级前一致）。
                if not ip_allowed(row.ip_allow, ip):
                    raise _ip_not_allowed()
                # user_id 显式置 None（而不是漏掉这个 key）：API Key 是集成方/MCP
                # 用的独立凭据，不代表某个具体登录用户，没有可归属的 user_id——
                # 这类 actor 建的会话落 NULL，语义上等同"历史遗留/无归属"，
                # 只有它自己和后续任何人都能在归属过滤下看到（见 _visible_filter）。
                actor = {"name": row.name, "role": row.role, "user_id": None,
                         # key_id：限流与用量按它归档（kbase/ratelimit.py）；
                         # rpm/daily_quota 仅供限流依赖读，不参与授权判定。
                         "key_id": row.id,
                         "rpm": row.rpm, "daily_quota": row.daily_quota}
                # 库级 scope（ztenith MCP）：JSON 数组→白名单进 actor，越权查询由
                # 查询路由静默空集处理；NULL/脏数据=不限（与升级前行为一致）。
                if row.scope_kb_ids:
                    try:
                        scope = json.loads(row.scope_kb_ids)
                        if isinstance(scope, list):
                            actor["scope_kb_ids"] = [str(x) for x in scope]
                    except (ValueError, TypeError):
                        pass
                # 最近使用时间：写侧节流到每 Key 每分钟至多一次（kbase/ratelimit.py
                # 记账）——鉴权在每请求的关键路径上，不能每个请求都写库。同一
                # 会话内顺手提交，不额外开一次连接。
                if ratelimit.limiter.allow_last_used_write(row.id):
                    row.last_used_at = datetime.utcnow()
                    s.commit()
            request.state.actor = actor
            return actor

        raise _unauthorized()

    return get_current_actor


def make_synthetic_admin_actor_dependency():
    """auth="off" 用的路由级依赖：不做任何凭据校验，直接把 request.state.actor
    设成一个 rank 最高的合成 actor（name=ANONYMOUS_ACTOR_NAME, role="superadmin"）。

    两个目的一次达成：
    - role 矩阵无操作——所有 require_role(min_role) 检查读到 admin rank，
      永远放行，off 模式下的既有功能测试行为不变；
    - 审计钩子仍能读到 request.state.actor，落到审计表的 actor 字段是
      ANONYMOUS_ACTOR_NAME，而不是错误地显示为 "admin" 这个真实用户名。
    """

    def _set_synthetic_actor(request: Request) -> dict:
        # user_id=None：off 模式没有真实用户体系，会话归属过滤（见
        # kbase/conversations.py）在这个模式下天然退化成"只看 NULL 归属的会话"
        # ——因为所有会话都会用这同一个合成 actor 创建，全部落 NULL，过滤条件
        # 因此对既有功能测试/单机免鉴权部署完全透明（大家看到的还是全部会话）。
        # role=superadmin：off 模式的合成 actor 必须是 rank 最高的角色，
        # 否则引入超管层级后本地免鉴权模式反而动不了用户管理（rank 不够）。
        actor = {"name": ANONYMOUS_ACTOR_NAME, "role": "superadmin", "user_id": None}
        request.state.actor = actor
        return actor

    return _set_synthetic_actor


def require_role(min_role: str, sf=None):
    """工厂：返回一个依赖，要求 request.state.actor 的角色满足 min_role
    对应的门槛。403 detail 用中文，前端可直接展示。

    两条判定路径（自定义角色 RBAC）：
    - **内置角色**（viewer/editor/admin/superadmin）走原 rank 序比较，行为与
      引入自定义角色前逐字节一致（零回归）；
    - **自定义角色**（users.role 存的是 roles 表里的角色名）走权限集合：
      min_role=editor 要 content.manage、admin 要 system.admin、viewer 只需
      登录。sf 为空（未注入 session factory）时自定义角色一律拒绝——宁可
      拒绝也不误放行。

    依赖 request.state.actor 已由路由级的 get_current_actor（auth="on"）
    或 synthetic_admin_actor（auth="off"）写入——require_role 本身不发起
    鉴权，因此可以在两种模式下用同一套路由级声明。"""
    from kbase.auth import roles as _roles

    min_rank = _ROLE_RANK[min_role]
    # 门槛 → 所需权限（viewer=登录即可，无需权限）
    needed_perm = {"editor": _roles.PERM_CONTENT,
                   "admin": _roles.PERM_SYSTEM}.get(min_role)

    def _check(request: Request) -> dict:
        actor = getattr(request.state, "actor", None)
        if actor is None:
            # 理论上不会发生：路由级鉴权依赖总是先于 require_role 执行并
            # 写好 request.state.actor；保留此分支只是防御性兜底。
            raise _unauthorized()
        role = actor["role"]
        if role in _ROLE_RANK:
            # 内置角色：原 rank 语义不变
            if _ROLE_RANK[role] < min_rank:
                raise HTTPException(status_code=403,
                                    detail="权限不足：当前角色无法执行此操作")
            return actor
        # 自定义角色：必须**确实存在**于 roles 表，再看权限集合。
        # 角色不存在（伪角色脏数据/已删除的角色）一律 403，连 viewer 门槛也
        # 不放行——纵深防御，与引入自定义角色前"未知角色 rank=-1 全拒"一致；
        # 零权限但已定义的角色则可过 viewer 门槛（问答），二者语义不同。
        # sf 未注入时无从查证，同样拒绝（不误放行）。
        if sf is None or not _roles.role_exists(sf, role):
            raise HTTPException(status_code=403,
                                detail="权限不足：当前角色无法执行此操作")
        if needed_perm is None:
            return actor          # viewer 门槛：角色已定义即可（含零权限角色）
        if needed_perm not in _roles.resolve_permissions(sf, role):
            raise HTTPException(status_code=403,
                                detail="权限不足：当前角色无法执行此操作")
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
