"""免登录分享域路由（对标清单 #1：嵌入 widget + 免登录分享链接）。

两组端点：
- 管理组（共享 router，editor+）：建链接/列表/撤销——模型（provider）在
  建链接侧绑定，对标 Dify WebApp/FastGPT 免登录窗的"构建者配置、使用者
  消费"模式；
- 公开组（app 级，无鉴权）：token 即授权。meta 取库名、query 走与登录端
  完全相同的 _run_query 编排（citations→token*→done 事件序列一致，引用/
  附图/拒答语义一致）。撤销即失效；未知/已撤销统一 404 不泄露存在性。
"""
import json
import mimetypes
import secrets
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from kbase.api.routes import RouteDeps
from kbase.api.schemas import QueryBody, ShareLinkCreate
from kbase.api.services import Services
from kbase.audit import write_audit
from kbase.errors import AppError
from kbase.models import Document, KnowledgeBase, ShareLink


def register(app: FastAPI, router, svc: Services, deps: RouteDeps,
             *, run_query) -> None:
    sf = svc.sf

    # ---- 管理组（editor+）----

    @router.post("/kb/{kb_id}/share-links",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def create_share_link(kb_id: str, body: ShareLinkCreate, request: Request):
        # 多库联查：路径主库 + extra_kb_ids 合并去重（主库恒为首项）；任一
        # 库不存在即 404——建链接时就挡住脏引用，而不是等匿名查询才发现。
        all_ids = list(dict.fromkeys([kb_id, *body.extra_kb_ids]))
        with sf() as s:
            for k in all_ids:
                if s.get(KnowledgeBase, k) is None:
                    raise AppError("error.kb_not_found", "知识库不存在: {id}", status=404, id=k)
        actor = getattr(request.state, "actor", None)
        row = ShareLink(id=str(uuid.uuid4()), kb_id=kb_id,
                        # 单库存 NULL（老行为字节级不变），联查才存 JSON
                        kb_ids=(json.dumps(all_ids) if len(all_ids) > 1 else None),
                        token=secrets.token_urlsafe(24),
                        name=body.name.strip(), provider=body.provider,
                        created_by=(actor["name"] if actor else None))
        with sf() as s:
            s.add(row)
            s.commit()
        return {"id": row.id, "token": row.token, "name": row.name,
                "provider": row.provider, "kb_ids": all_ids}

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
    def list_share_links(kb_id: str):
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
                            "created_at": r.created_at.isoformat()})
            return out

    @router.delete("/share-links/{link_id}",
                   dependencies=[deps.require_editor, deps.audit_mutation])
    def revoke_share_link(link_id: str):
        with sf() as s:
            row = s.get(ShareLink, link_id)
            if row is None:
                raise AppError("error.share_link_not_found", "分享链接不存在: {id}", status=404, id=link_id)
            row.revoked = True     # 软删：审计可查，公开端点立即拒绝
            s.commit()
        return {"ok": True}

    # ---- 公开组（app 级，token 即授权）----

    def _resolve(token: str) -> ShareLink:
        with sf() as s:
            row = s.query(ShareLink).filter_by(token=token,
                                               revoked=False).first()
            if row is None:
                raise AppError("error.share_link_invalid", "分享链接不存在或已失效", status=404)
            if s.get(KnowledgeBase, row.kb_id) is None:
                raise AppError("error.share_link_invalid", "分享链接不存在或已失效", status=404)
            s.expunge(row)
        return row

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
    def share_meta(token: str):
        """分享页首屏：库名（终端用户知道自己在问什么范围）。kb_name 为主库
        名（向后兼容），kb_names 为联查全量（单库时长度 1）。"""
        link = _resolve(token)
        kbs = _live_kbs(link)
        return {"kb_name": kbs[0][1], "name": link.name,
                "kb_names": [n for _, n in kbs]}

    @app.get("/api/share/{token}/images/{doc_id}/{filename}")
    def share_image(token: str, doc_id: str, filename: str):
        """回答附图的免登录直链：/api/documents/... 是鉴权端点，匿名访客
        取图 401 裂图（真机踩中）。校验链：token 有效 → 文档属于该链接
        绑定的库 → 纯文件名（防 ../ 穿越）→ 出图。"""
        link = _resolve(token)
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
        模型是建链接侧的决策）；事件流与登录端 query 完全一致。"""
        link = _resolve(token)
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
