"""免登录分享域路由（对标清单 #1：嵌入 widget + 免登录分享链接）。

两组端点：
- 管理组（共享 router，editor+）：建链接/列表/撤销——模型（provider）在
  建链接侧绑定，对标 Dify WebApp/FastGPT 免登录窗的"构建者配置、使用者
  消费"模式；
- 公开组（app 级，无鉴权）：token 即授权。meta 取库名、query 走与登录端
  完全相同的 _run_query 编排（citations→token*→done 事件序列一致，引用/
  附图/拒答语义一致）。撤销即失效；未知/已撤销统一 404 不泄露存在性。

T10：链接可设有效期/访问口令/次数上限。失效（不存在/已撤销/主库被删/过期/
次数用尽）**一律 404 share_link_invalid**——不区分原因，免得凭错误码探出
"链接是存在的，只是过期了"；唯独口令不对给 401（前端靠它弹口令框，而不是
把链接判死）。计次只算免登录问答（meta/附图不算），走"条件自增 + 数影响行数"
的原子 UPDATE，并发下不越上限。三个公开端点注册在 app 级、过不了 /api router
的依赖链，T09 的路由级限流覆盖不到——这里手动调用同一份进程内滑窗补上。
"""
import json
import mimetypes
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, update

from kbase.api.guards import KbGuard
from kbase.api.routes import RouteDeps
from kbase.api.schemas import QueryBody, ShareLinkCreate
from kbase.api.services import Services
from kbase.audit import write_audit
from kbase.auth.security import hash_password, verify_password
from kbase.errors import AppError
from kbase.models import Document, KnowledgeBase, ShareLink
from kbase.ratelimit import limiter, utc_day

# T10：公开端点限流阈值（每分钟）。复用 T09 的进程内滑窗（kbase/ratelimit.py
# 的 limiter 单例）——同一套实现、同一份状态、同样的 429 + Retry-After 形状。
# 两道闸独立计数：token 一道（换 IP 绕不过，也是真正管住 LLM 成本的那道）+
# 来源 IP 一道（换 token 刷也绕不过，阈值放宽，只当洪水闸）。三个公开端点共用
# 一个桶——否则匿名访客猛刷附图就等于绕过问答限流。
# IP 取传输层对端（与 T09/deps.py、登录审计同一口径，**不信任
# X-Forwarded-For**）：部署在未透传真实来源的反代后面时，它看到的是代理地址，
# 这道闸实际退化成"整个出口"的闸——阈值放宽正是为此留的余量。
# 口径与 T09 完全一致：lite 单进程精确，standard 多 worker 为每进程近似。
_SHARE_TOKEN_RPM = 60
_SHARE_IP_RPM = 240

# T10：口令走请求头而不是 query string——query 会进 Nginx/网关访问日志，
# 口令落进日志就等于泄漏。前端（分享页）拿到 401 后弹框，输入值放这个头。
SHARE_PASSWORD_HEADER = "X-Share-Password"


def _naive_utc(dt: datetime | None) -> datetime | None:
    """入参一律收成 naive UTC（与 created_at 同口径，见 models.py）：前端
    toISOString() 带 Z 时先转 UTC 再去 tzinfo，naive 视为已是 UTC。"""
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime | None) -> str | None:
    """naive UTC datetime → ISO 串（管理端列表/创建响应），None 原样返回。"""
    return dt.isoformat() if dt else None


def _rate_limit_public(request: Request, token: str) -> None:
    """T10：公开分享端点的限流（手动调用 T09 的进程内滑窗）。

    公开端点注册在 app 级、没有 actor，T09 挂在 /api 与 /v1 router 上的限流
    依赖根本不会执行——不补这一手，持链接者就能无限刷（每次问答都是一次真实
    LLM 调用）。key 加命名空间前缀，避免与 limiter 里按 API Key id 归档的桶撞。
    """
    now = limiter.clock()
    day = utc_day(now)
    retry_after = limiter.check(f"share:{token}", rpm=_SHARE_TOKEN_RPM,
                                day=day, now=now)
    client = request.client
    if not retry_after and client is not None and client.host:
        retry_after = limiter.check(f"share-ip:{client.host}", rpm=_SHARE_IP_RPM,
                                    day=day, now=now)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited",
                    "message": f"分享链接请求过于频繁，请在 {retry_after} 秒后重试"},
            headers={"Retry-After": str(retry_after)})


def register(app: FastAPI, router, svc: Services, deps: RouteDeps,
             *, run_query) -> None:
    sf = svc.sf
    # T02/G09：管理组端点按链接绑定的 kb 过 ACL + scope（修前建链接只校验
    # 库是否存在——拿到 kb_id 就能把无权库挂进免登录链接，绕开 ACL 把内容
    # 对外发出去，比单个文档越权读更严重）。
    guard = KbGuard(sf)

    # ---- 管理组（editor+）----

    @router.post("/kb/{kb_id}/share-links",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def create_share_link(kb_id: str, body: ShareLinkCreate, request: Request):
        # 多库联查：路径主库 + extra_kb_ids 合并去重（主库恒为首项）；任一
        # 库不存在**或建链人无权**（ACL/scope）即 404——建链接时就挡住脏引用
        # 与越权夹带，而不是等匿名查询才发现。
        all_ids = list(dict.fromkeys([kb_id, *body.extra_kb_ids]))
        for k in all_ids:
            guard.kb(k, request)
        with sf() as s:
            missing = [k for k in all_ids if s.get(KnowledgeBase, k) is None]
        if missing:
            raise AppError("error.kb_not_found", "知识库不存在: {id}",
                           status=404, id=missing[0])
        actor = getattr(request.state, "actor", None)
        row = ShareLink(id=str(uuid.uuid4()), kb_id=kb_id,
                        # 单库存 NULL（老行为字节级不变），联查才存 JSON
                        kb_ids=(json.dumps(all_ids) if len(all_ids) > 1 else None),
                        token=secrets.token_urlsafe(24),
                        name=body.name.strip(), provider=body.provider,
                        created_by=(actor["name"] if actor else None),
                        # T10：策略字段一律 NULL=该维度不限（不填即老行为）。
                        # 口令只落 bcrypt 哈希，明文既不落库也不回传（连哈希都
                        # 不回传，只回 has_password 供前端显示"需口令"徽标）。
                        expires_at=_naive_utc(body.expires_at),
                        password_hash=(hash_password(body.password)
                                       if body.password else None),
                        max_visits=body.max_visits)
        with sf() as s:
            s.add(row)
            s.commit()
        return {"id": row.id, "token": row.token, "name": row.name,
                "provider": row.provider, "kb_ids": all_ids,
                "expires_at": _iso(row.expires_at),
                "has_password": row.password_hash is not None,
                "max_visits": row.max_visits, "visit_count": row.visit_count}

    def _link_kb_ids(row: ShareLink) -> list[str]:
        """链接绑定的全部库 id（主库恒为首项）。kb_ids 为 NULL=单库老数据。"""
        if row.kb_ids:
            try:
                ids = json.loads(row.kb_ids)
                if isinstance(ids, list) and ids:
                    return list(dict.fromkeys([row.kb_id, *ids]))
            except ValueError:
                pass    # 脏 JSON 不炸公开端点，退回单库
        return [row.kb_id]

    @router.get("/kb/{kb_id}/share-links", dependencies=[deps.require_editor])
    def list_share_links(kb_id: str, request: Request):
        # token 对建链接的人不是秘密，但仍只给有权访问该库的人看列表
        guard.kb(kb_id, request)
        # token 对建链接的人不是秘密（列表就是为了复制分发），完整返回。
        # kb_names 供管理列表显示联查范围（已删副库名自然缺席）。
        with sf() as s:
            rows = (s.query(ShareLink).filter_by(kb_id=kb_id, revoked=False)
                    .order_by(ShareLink.created_at.desc()).all())
            out = []
            for r in rows:
                ids = _link_kb_ids(r)
                kbs = [s.get(KnowledgeBase, k) for k in ids]
                out.append({"id": r.id, "token": r.token, "name": r.name,
                            "provider": r.provider,
                            "kb_ids": ids,
                            "kb_names": [kb.name for kb in kbs if kb is not None],
                            # T10：策略与用量。**绝不返回 password_hash**——
                            # 前端只要知道"这条链接需不需要口令"。
                            "expires_at": _iso(r.expires_at),
                            "has_password": r.password_hash is not None,
                            "max_visits": r.max_visits,
                            "visit_count": r.visit_count,
                            "created_at": r.created_at.isoformat()})
            return out

    @router.delete("/share-links/{link_id}",
                   dependencies=[deps.require_editor, deps.audit_mutation])
    def revoke_share_link(link_id: str, request: Request):
        with sf() as s:
            row = s.get(ShareLink, link_id)
            if row is None:
                raise AppError("error.share_link_not_found", "分享链接不存在: {id}", status=404, id=link_id)
            row.revoked = True     # 软删：审计可查，公开端点立即拒绝
            s.commit()
        # 撤销按绑定库过 ACL + scope（联查链接任一绑定库有权即可撤）
        for k in _link_kb_ids(row):
            if guard.allows(k, request):
                break
        else:
            raise AppError("error.share_link_not_found", "分享链接不存在: {id}", status=404, id=link_id)
        return {"ok": True}

    # ---- 公开组（app 级，token 即授权）----

    def _invalid() -> AppError:
        """失效链接的统一出口：不存在/已撤销/主库被删/过期/次数用尽都走这里
        ——同码同文案，访客与探测者都分不出是哪一种（T10 新增后三种）。"""
        return AppError("error.share_link_invalid", "分享链接不存在或已失效",
                        status=404)

    def _resolve(token: str, request: Request, *, capacity: bool = True) -> ShareLink:
        """token → 链接行。T10 校验顺序：身份 → 有效期 → 口令 → 次数容量。

        口令是**唯一可区分**的失败（401）：前端要靠它弹口令框；缺头与错口令
        同码同文案（对访客而言都是"要口令"）。次数容量在这里只是"提前拒"：
        真正的扣次在 share_query 的原子 UPDATE 里，这里让已用尽的链接在首屏
        （meta）就被挡住，不白跑一次问答编排。
        capacity=False 只跳过后一项（附图端点专用，理由见 share_image）。
        """
        with sf() as s:
            row = s.query(ShareLink).filter_by(token=token,
                                               revoked=False).first()
            if row is None:
                raise _invalid()
            if s.get(KnowledgeBase, row.kb_id) is None:
                raise _invalid()
            # T10：NULL=永不过期（老库补列即 NULL，行为与升级前一致）。
            # naive UTC 比较，与写入口径一致（_naive_utc）。
            if (row.expires_at is not None
                    and row.expires_at <= datetime.utcnow()):
                raise _invalid()
            # T10：NULL=免口令。口令错单独 401——前端据此弹口令输入框重试，
            # 而不是把整条链接判死。
            if row.password_hash:
                supplied = request.headers.get(SHARE_PASSWORD_HEADER) or ""
                try:
                    # 空口令直接短路，不白跑一次 bcrypt
                    ok = bool(supplied) and verify_password(supplied,
                                                            row.password_hash)
                except ValueError:
                    ok = False      # 脏哈希（人工改库）不炸公开端点，按口令错处理
                if not ok:
                    raise AppError("error.share_password_required",
                                   "该分享链接需要访问口令", status=401)
            # T10：visit_count=NULL 视作 0（老链接被补设上限后仍能正确判定）。
            if (capacity and row.max_visits is not None
                    and (row.visit_count or 0) >= row.max_visits):
                raise _invalid()
            s.expunge(row)
        return row

    def _consume_visit(token: str) -> None:
        """T10：原子占用一次访问额度（只有免登录问答计次，meta/附图不计——
        访客看一眼库名、取一张附图不算"访问"）。

        判定与自增必须在同一条 UPDATE 里、再看影响行数：先 SELECT 再 UPDATE
        在两个并发请求之间有窗口，双双读到"还没满"就会双双放行（次数上限
        形同虚设）。写库的行锁把并发 UPDATE 串行化，超限的那几条影响 0 行
        → 一律 404。
        visit_count 用 COALESCE 归一：老库该列是 NULL，直接 NULL+1 还是 NULL
        （计数被写没），而 NULL < n 也不成立（老链接补设上限后永远扣不动）
        ——两种毛病靠它一起避开。max_visits=NULL 时 WHERE 恒真 = 不限次数。
        扣次在问答编排**之前**：一次提问算一次访问，中途失败（上游报错/访客
        刷新中断）照算——事后回滚得等流结束再判定，那时回答已经发给访客了。
        """
        stmt = (
            update(ShareLink)
            .where(ShareLink.token == token,
                   ShareLink.revoked.is_(False),
                   or_(ShareLink.max_visits.is_(None),
                       func.coalesce(ShareLink.visit_count, 0)
                       < ShareLink.max_visits))
            .values(visit_count=func.coalesce(ShareLink.visit_count, 0) + 1)
            # 纯 Core UPDATE，不回写会话里的对象（避免 ORM 同步把并发值覆盖）
            .execution_options(synchronize_session=False)
        )
        with sf() as s:
            affected = s.execute(stmt).rowcount
            s.commit()
        if affected == 0:
            raise _invalid()

    def _live_kbs(link: ShareLink) -> list[tuple[str, str]]:
        """链接绑定库中仍存在的 (id, name)，主库恒在首位（_resolve 已保证
        主库活着）。联查副库被删=静默从检索范围剔除，链接本身不死。"""
        out: list[tuple[str, str]] = []
        with sf() as s:
            for k in _link_kb_ids(link):
                kb = s.get(KnowledgeBase, k)
                if kb is not None:
                    out.append((k, kb.name))
        return out

    @app.get("/api/share/{token}")
    def share_meta(token: str, request: Request):
        """分享页首屏：库名（终端用户知道自己在问什么范围）。kb_name 为主库
        名（向后兼容），kb_names 为联查全量（单库时长度 1）。T10：首屏就校验
        有效期/口令/次数（口令链接在此拿 401，前端弹框），但**不计次**。"""
        _rate_limit_public(request, token)
        link = _resolve(token, request)
        kbs = _live_kbs(link)
        return {"kb_name": kbs[0][1], "name": link.name,
                "kb_names": [n for _, n in kbs]}

    @app.get("/api/share/{token}/images/{doc_id}/{filename}")
    def share_image(token: str, doc_id: str, filename: str, request: Request):
        """回答附图的免登录直链：/api/documents/... 是鉴权端点，匿名访客
        取图 401 裂图（真机踩中）。校验链：token 有效（T10：含有效期/口令，
        口令链接取图同样要带头）→ 文档属于该链接绑定的库 → 纯文件名（防 ../
        穿越）→ 出图。T10：出图**不计次**，一次回答的多张附图不等于多次访问；
        次数闸也**只管"还能不能继续问"**（capacity=False）——否则最后一次问答
        刚把额度用光，那条回答自己的插图就会 404 裂图。附图不计次，也不因此
        多暴露任何东西：URL 本来就随那条回答给出去了。"""
        _rate_limit_public(request, token)
        link = _resolve(token, request, capacity=False)
        # 联查链接：任一绑定库（仍存在的）的文档附图都可出——回答可能引用
        # 副库文档，其插图同样要免登录可见
        allowed = {k for k, _ in _live_kbs(link)}
        with sf() as s:
            doc = s.get(Document, doc_id)
            if doc is None or doc.kb_id not in allowed:
                raise AppError("error.image_not_found", "图片不存在", status=404)
        safe = Path(filename).name
        if safe != filename or not safe:
            raise AppError("error.image_not_found", "图片不存在", status=404)
        img_path = svc.cfg.data_dir / "files" / doc_id / "images" / safe
        if not img_path.is_file():
            raise AppError("error.image_not_found", "图片不存在", status=404)
        media_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        return FileResponse(str(img_path), media_type=media_type)

    @app.post("/api/share/{token}/query")
    async def share_query(token: str, body: QueryBody, request: Request):
        """免登录问答：provider 以链接绑定为准（终端用户传什么都不认——
        模型是建链接侧的决策）；事件流与登录端 query 完全一致。
        T10：三个公开端点里**只有这里计次**——一次问答=一次访问，上限用尽
        的链接在这里被原子扣次挡住（404），不会越过 max_visits。"""
        _rate_limit_public(request, token)
        link = _resolve(token, request)
        _consume_visit(token)
        body.provider = link.provider           # None=系统默认
        # 多库联查：仍存在的绑定库 >1 时传 kb_ids 走 retrieve_multi 散射聚合
        # （与登录端会话联查同一条路，M6-2）；单库传 None 保持既有路径字节级不变。
        live_ids = [k for k, _ in _live_kbs(link)]
        client = request.client
        write_audit(sf, actor=f"share:{token[:8]}", action="share_query",
                    # 单库保持老格式（既有审计消费方零变化），联查才用复数键
                    resource=(f"kb_id={link.kb_id}" if len(live_ids) == 1
                              else f"kb_ids={','.join(live_ids)}"),
                    detail=body.question[:100],
                    ip=(client.host if client else None))
        return await run_query(link.kb_id, body, request=request,
                               kb_ids=(live_ids if len(live_ids) > 1 else None))
