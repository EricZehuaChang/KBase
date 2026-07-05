"""审计日志写入：write_audit 落一行到 audit_logs 表。

调用方（api/main.py 的 audit 依赖钩子、login 路由）负责决定何时落审计行；
本模块只管持久化，不做业务判断。detail 是自由格式 dict，这里统一转 JSON
字符串存储，并按 spec 截断，防止异常调用把超大内容塞进审计表。
"""
import json
import uuid
from datetime import datetime

from fastapi import Request

from kbase.models import AuditLog

# detail 落库前的最大字符数——审计是留痕不是全量日志，超长内容截断即可。
_DETAIL_MAX_CHARS = 2000


def write_audit(sf, actor: str, action: str, resource: str | None = None,
                detail: dict | str | None = None, ip: str | None = None) -> None:
    """写一行审计记录。

    actor: 用户名或 API Key name（G2 的 get_current_actor 统一表示的 name）。
    action: 形如 "POST /api/kb/{kb_id}/documents" 或 "login_success"/"login_failed"/"query" 的动作标签。
    resource: 资源标识（如 kb_id/doc_id），可为 None（如登录事件无具体资源）。
    detail: 附加上下文（如问题前100字、请求路径参数），dict 会被序列化成 JSON 字符串；
    过长时截断到 _DETAIL_MAX_CHARS 字符。
    ip: 客户端 IP，取自 request.client.host，可能为 None（测试环境等）。
    """
    if isinstance(detail, dict):
        detail_str = json.dumps(detail, ensure_ascii=False)
    else:
        detail_str = detail
    if detail_str is not None and len(detail_str) > _DETAIL_MAX_CHARS:
        detail_str = detail_str[:_DETAIL_MAX_CHARS]

    with sf() as s:
        s.add(AuditLog(
            id=str(uuid.uuid4()), ts=datetime.utcnow(), actor=actor,
            action=action, resource=resource, detail=detail_str, ip=ip))
        s.commit()


def make_mutation_audit_dependency(sf):
    """返回一个 FastAPI 依赖：记录一次 mutating 请求（method+路由路径模板作为
    action，路径参数作为 resource）。挂在各 mutating 路由的 dependencies=[...]
    列表里（在 require_role 之后，确保先过权限校验再落审计行——被 403 拒绝的
    请求不产生审计行，只有真正被允许执行的操作才留痕）。

    action 用路由路径模板而不是拼好参数的实际路径，例如
    "DELETE /api/kb/{kb_id}/documents/{doc_id}"——这样同一类操作在审计表里
    聚成一个 action 值，便于按操作类型筛选，而不是每个具体 kb_id 各是一行
    不同的 action。resource 则是拼接的路径参数（如 "kb_id=xxx,doc_id=yyy"），
    留下具体是哪个资源被改动。

    actor 读 request.state.actor（必须由本请求更早的鉴权/合成 actor 依赖写入，
    FastAPI 依赖按声明顺序解析，这里假设它总在 require_role 之后被 Depends）。
    """

    def _record(request: Request) -> None:
        actor = getattr(request.state, "actor", None)
        actor_name = actor["name"] if actor else "unknown"
        route = request.scope.get("route")
        path_template = route.path if route is not None else request.url.path
        action = f"{request.method} {path_template}"
        resource = (",".join(f"{k}={v}" for k, v in request.path_params.items())
                    or None)
        client = request.client
        ip = client.host if client is not None else None
        write_audit(sf, actor=actor_name, action=action, resource=resource, ip=ip)

    return _record


# 问答审计 detail 里问题文本只留前 N 字（spec §5："query 记问题前100字"）——
# 这个截断比 write_audit 通用的 _DETAIL_MAX_CHARS 更严格，是刻意的：审计表
# 里的问答记录只是留痕方便追溯"谁问了什么"，不需要完整问题内容。
_QUERY_DETAIL_CHARS = 100


def write_query_audit(sf, request: Request, resource: str, question: str) -> None:
    """问答/会话查询专用审计写入：action 固定为 "query"，detail 是问题前
    _QUERY_DETAIL_CHARS 字符。actor 读 request.state.actor（由路由级鉴权/
    合成 actor 依赖写入）。挂在 /kb/{kb_id}/query 与
    /conversations/{conv_id}/query 两个端点里，在检索/生成开始前调用——
    审计的是"发起了这次问答"这个动作本身，不依赖生成是否成功。"""
    actor = getattr(request.state, "actor", None)
    actor_name = actor["name"] if actor else "unknown"
    client = request.client
    ip = client.host if client is not None else None
    write_audit(sf, actor=actor_name, action="query", resource=resource,
               detail=question[:_QUERY_DETAIL_CHARS], ip=ip)


def list_audit(sf, limit: int = 50, offset: int = 0) -> dict:
    """分页读取审计行，按时间倒序（最新的在前）。返回 {items, total}。"""
    with sf() as s:
        total = s.query(AuditLog).count()
        rows = (s.query(AuditLog)
                .order_by(AuditLog.ts.desc())
                .offset(offset).limit(limit).all())
        items = [{"id": r.id, "ts": r.ts.isoformat(), "actor": r.actor,
                  "action": r.action, "resource": r.resource,
                  "detail": r.detail, "ip": r.ip} for r in rows]
    return {"items": items, "total": total}
