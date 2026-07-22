"""知识库与文档域路由：kb 增删查、kb 级配置、文档上传/列表/全文/删除/重试。

删除类端点的级联顺序是硬约束（向量 → 全文索引 → 文件目录 → DB 行），
注释随各端点保留；摄取一律走后台任务（BackgroundTasks），批次内并行度由
cfg.ingest.workers 控制。"""
import json
import mimetypes
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import (BackgroundTasks, Form, HTTPException, Query, Request,
                     UploadFile)
from fastapi.responses import FileResponse

from kbase import chunk_admin, kb_acl
from kbase.api.routes import RouteDeps
from kbase.api.schemas import (ChunkUpdate, DocumentReview, FeishuImportBody,
                               KBConfigBody, KBCreate, KbGrantsBody,
                               RebindEmbedderBody, UrlImportBody)
from kbase.api.services import Services
from kbase.errors import AppError
from kbase.models import (Chunk, Connector, ConnectorDoc, Conversation,
                          Document, DocumentImage, KnowledgeBase, Message)


_URL_MAX_BYTES = 10 * 1024 * 1024   # URL 导入响应体上限（M6-7）
_URL_TEXT_TYPES = ("text/html", "text/plain", "text/markdown",
                   "application/xhtml+xml")


def _fetch_url(url: str) -> tuple[bytes, str]:
    """拉取网页正文（M6-7）。模块级函数便于测试打桩。
    返回 (内容字节, 建议文件名)；不合规输入抛 HTTPException。"""
    import httpx
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise AppError("error.only_http", "仅支持 http/https 地址: {url}", status=422, url=url)
    try:
        resp = httpx.get(url, timeout=20.0, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"URL 拉取失败: {e}") from e
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype and not any(ctype.startswith(t) for t in _URL_TEXT_TYPES):
        raise AppError("error.unsupported_content", "不支持的内容类型 {type}（只收网页/文本）", status=422, type=ctype)
    content = resp.content[:_URL_MAX_BYTES]
    # 文件名：host + path 末段做 slug，落 .html/.md 后缀给 markitdown 认
    tail = Path(parsed.path).name or "index"
    suffix = ".md" if ctype in ("text/plain", "text/markdown") else ".html"
    if not tail.endswith((".html", ".htm", ".md", ".txt")):
        tail = f"{tail}{suffix}"
    return content, f"{parsed.netloc.replace(':', '_')}-{tail}"


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf, cfg, store, keyword_index, pipeline = (
        svc.sf, svc.cfg, svc.store, svc.keyword_index, svc.pipeline)

    @router.get("/embedders", dependencies=[deps.require_viewer])
    def list_embedders():
        """建库下拉用：默认向量模型 + cfg.embedders 可选清单（M5-2）。"""
        return svc.embedder_catalog

    # ---- 库级权限管理（M6-3，admin）----

    @router.get("/kb/{kb_id}/grants", dependencies=[deps.require_admin])
    def get_kb_grants(kb_id: str):
        """某库授权用户清单；空=公开库（所有登录用户可见）。"""
        with sf() as s:
            if s.get(KnowledgeBase, kb_id) is None:
                raise AppError("error.kb_not_found", "知识库不存在: {id}", status=404, id=kb_id)
        return {"grants": kb_acl.list_grants(sf, kb_id)}

    @router.put("/kb/{kb_id}/grants",
                dependencies=[deps.require_admin, deps.audit_mutation])
    def put_kb_grants(kb_id: str, body: KbGrantsBody):
        """全量设置授权用户集合（空列表=恢复公开）。"""
        with sf() as s:
            if s.get(KnowledgeBase, kb_id) is None:
                raise AppError("error.kb_not_found", "知识库不存在: {id}", status=404, id=kb_id)
        kb_acl.set_grants(sf, kb_id, body.user_ids)
        return {"ok": True, "count": len(set(body.user_ids))}

    @router.post("/kb", dependencies=[deps.require_editor, deps.audit_mutation])
    def create_kb(body: KBCreate, request: Request):
        # 绑定校验先行：向量模型 id 必须在配置清单内，建完不可改
        #（换模型=向量空间不可比，该库全部向量作废，只能重建）。
        embedder_id = body.embedder or "default"
        if embedder_id not in svc.embedder_ids:
            raise HTTPException(
                422, f"未知的向量模型: {embedder_id}，可选: {sorted(svc.embedder_ids)}")
        config = (json.dumps({"embedder": embedder_id}, ensure_ascii=False)
                  if embedder_id != "default" else None)
        actor = getattr(request.state, "actor", None)
        kb = KnowledgeBase(id=str(uuid.uuid4()), name=body.name, config=config,
                           owner_id=(actor.get("user_id") if actor else None))
        with sf() as s:
            s.add(kb)
            s.commit()
        return {"id": kb.id, "name": kb.name, "embedder": embedder_id}

    @router.get("/kb", dependencies=[deps.require_viewer])
    def list_kb(request: Request):
        # M6-3 库级权限：按 ACL 过滤——admin 全见；其余仅见公开库（无授权
        # 记录）+ 被授权的库 + 自己建的库。
        actor = getattr(request.state, "actor", None) or {"role": "admin"}
        mode, visible = kb_acl.visible_kb_filter(sf, actor)
        with sf() as s:
            # doc_count 随列表一次返回（单条 group by）：此前前端逐库拉文档
            # 列表数数，既是 N+1 请求，又让卡片计数先显示 0 再跳变。
            from sqlalchemy import func
            counts = dict(s.query(Document.kb_id, func.count(Document.id))
                          .group_by(Document.kb_id).all())
            return [{"id": k.id, "name": k.name,
                     "doc_count": counts.get(k.id, 0),
                     "config": json.loads(k.config) if k.config else None}
                    for k in s.query(KnowledgeBase).all()
                    if mode == "all" or k.id in visible]

    @router.delete("/kb/{kb_id}", dependencies=[deps.require_editor, deps.audit_mutation])
    def delete_kb(kb_id: str):
        with sf() as s:
            kb = s.get(KnowledgeBase, kb_id)
            if kb is None:
                raise AppError("error.kb_not_found", "知识库不存在: {id}", status=404, id=kb_id)
            doc_ids = [d.id for d in s.query(Document).filter_by(kb_id=kb_id).all()]
            conv_ids = [c.id for c in s.query(Conversation).filter_by(kb_id=kb_id).all()]
        # 级联顺序：向量集合 → 全文索引 → files 目录 → chunks/document_images
        # /documents 行 → conversations/messages 行 → kb 行
        store.delete_collection(kb_id)
        if keyword_index is not None:
            keyword_index.delete_kb(kb_id)
        for doc_id in doc_ids:
            shutil.rmtree(cfg.data_dir / "files" / doc_id, ignore_errors=True)
        with sf() as s:
            s.query(Chunk).filter_by(kb_id=kb_id).delete()
            if doc_ids:
                s.query(DocumentImage).filter(
                    DocumentImage.doc_id.in_(doc_ids)).delete(
                    synchronize_session=False)
            s.query(Document).filter_by(kb_id=kb_id).delete()
            if conv_ids:
                s.query(Message).filter(Message.conv_id.in_(conv_ids)).delete(
                    synchronize_session=False)
            s.query(Conversation).filter_by(kb_id=kb_id).delete()
            # 同步连接器随库删除（对标#3）：孤儿连接器会被调度器继续触发、
            # 对已不存在的库反复同步失败
            conn_ids = [c.id for c in
                        s.query(Connector).filter_by(kb_id=kb_id).all()]
            if conn_ids:
                s.query(ConnectorDoc).filter(
                    ConnectorDoc.connector_id.in_(conn_ids)).delete(
                    synchronize_session=False)
            s.query(Connector).filter_by(kb_id=kb_id).delete()
            kb = s.get(KnowledgeBase, kb_id)
            if kb is not None:
                s.delete(kb)
            s.commit()
        # 二次清理：若行删除期间有摄取在途 upsert 重建了集合，扫掉这批孤儿向量
        store.delete_collection(kb_id)
        return {"ok": True}

    @router.post("/kb/{kb_id}/rebind-embedder",
                 dependencies=[deps.require_admin, deps.audit_mutation])
    def rebind_embedder(kb_id: str, body: RebindEmbedderBody, bg: BackgroundTasks):
        """换绑向量模型（vault 待办：换绑=触发全库重建的引导流程）。
        admin 门槛：全库向量作废+按新模型重嵌入是重操作（万块级分钟-小时）。
        流程：校验→更新绑定→后台[删旧collection→reindex_kb 存量chunk重嵌入]；
        重嵌入基于 DB 存量文本，不重新解析原始文件（不产生 OCR 费用）。
        新模型实例**先行构建**（密钥缺失当场 503，而不是绑定改完了后台才炸
        ——那会留下"绑定已换但向量还是旧空间"的坏状态）。"""
        from kbase.plugins.embedders.factory import kb_embedder_id
        from kbase.reindex import reindex_kb
        with sf() as s:
            if s.get(KnowledgeBase, kb_id) is None:
                raise AppError("error.kb_not_found", "知识库不存在: {id}", status=404, id=kb_id)
        if body.embedder not in svc.embedder_ids:
            raise HTTPException(
                422, f"未知的向量模型: {body.embedder}，可选: {sorted(svc.embedder_ids)}")
        current = kb_embedder_id(sf, kb_id) or "default"
        if body.embedder == current:
            raise AppError("error.kb_already_bound", "该库已绑定 {current}，无需换绑", status=409, current=current)
        try:
            new_embedder = svc.embedder_pool.get(body.embedder)
        except (RuntimeError, ValueError, KeyError) as e:
            raise HTTPException(503, f"新向量模型不可用（先配好密钥/端点）: {e}") from e

        with sf() as s:
            kb = s.get(KnowledgeBase, kb_id)
            try:
                config = json.loads(kb.config) if kb.config else {}
            except (json.JSONDecodeError, TypeError):
                config = {}
            if body.embedder == "default":
                config.pop("embedder", None)
            else:
                config["embedder"] = body.embedder
            kb.config = json.dumps(config, ensure_ascii=False) if config else None
            s.commit()

        def _rebuild() -> None:
            store.delete_collection(kb_id)     # 旧向量空间整体作废
            reindex_kb(sf, keyword_index, new_embedder, store, kb_id)

        bg.add_task(_rebuild)
        return {"ok": True, "from": current, "to": body.embedder}

    @router.put("/kb/{kb_id}/config", dependencies=[deps.require_editor, deps.audit_mutation])
    def put_kb_config(kb_id: str, body: KBConfigBody):
        with sf() as s:
            kb = s.get(KnowledgeBase, kb_id)
            if kb is None:
                raise AppError("error.kb_not_found", "知识库不存在: {id}", status=404, id=kb_id)
            # KBConfigBody 只管分块/增强字段；embedder 绑定（M5-2）是建库时
            # 定死的，必须从旧 config 原样带过来——否则一次分块参数调整就会把
            # 绑定冲掉、让该库静默回落默认模型（检索打分随即失效）。
            new_config = body.model_dump(exclude_none=True)
            try:
                old_config = json.loads(kb.config) if kb.config else {}
            except (json.JSONDecodeError, TypeError):
                old_config = {}
            if "embedder" in old_config:
                new_config["embedder"] = old_config["embedder"]
            kb.config = json.dumps(new_config, ensure_ascii=False)
            s.commit()
        return {"ok": True}

    def _ingest_batch(kb_id: str, items: list[tuple[Path, str]],
                      parse_mode: str = "auto") -> None:
        """单入口 bg task：ThreadPoolExecutor 并行摄取本批次所有文件（D5）。
        map 是惰性迭代器，必须消费完（list()）才会真正拉起所有任务并等待
        结果；executor 用 with 块在函数返回前 shutdown(wait=True)，保证
        TestClient 的同步 BackgroundTasks 语义下，响应返回时全部文件已经
        摄取完毕（否则测试断言文档状态会在 executor 还没跑完时就检查）。"""
        with ThreadPoolExecutor(max_workers=cfg.ingest.workers) as executor:
            list(executor.map(
                lambda item: pipeline.ingest_file(kb_id, item[0], item[1],
                                                  parse_mode=parse_mode),
                items))

    @router.post("/kb/{kb_id}/documents", dependencies=[deps.require_editor, deps.audit_mutation])
    def upload(kb_id: str, files: list[UploadFile], bg: BackgroundTasks,
               parse_mode: str = Form("auto")):
        """parse_mode（F）：auto=既有管道；ocr=表格增强（文本层 PDF 也强制
        GLM-OCR 结构化解析，跨页断表可合并，代价是丢页码定位）；vlm=满血
        视觉模型深度识别（仅图片格式生效，识别后停 pending_review 等人工
        校验）。非适用文件类型自动回落 auto 管道。批量上传共用同一模式。"""
        if parse_mode not in ("auto", "ocr", "vlm"):
            raise AppError("error.unknown_parse_mode", "未知的 parse_mode: {mode}", status=422, mode=parse_mode)
        upload_dir = cfg.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        accepted = []
        items: list[tuple[Path, str]] = []
        for f in files:
            safe_name = Path(f.filename or "unnamed").name   # 去除路径分隔符，防穿越；兜底 None
            dest = upload_dir / f"{uuid.uuid4()}-{safe_name}"
            dest.write_bytes(f.file.read())
            items.append((dest, safe_name))
            accepted.append(safe_name)
        bg.add_task(_ingest_batch, kb_id, items, parse_mode)
        return {"accepted": accepted}

    @router.post("/kb/{kb_id}/import-url",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def import_url(kb_id: str, body: UrlImportBody, bg: BackgroundTasks):
        """URL 连接器（M6-7）：拉取网页 → 存 .html → 走既有摄取管道
        （markitdown 的 HTML 转换器提正文）。私有化部署内网 wiki/门户是
        主用途，故不封内网地址；但 scheme 只收 http/https（file:// 等直接
        422），响应限 text 类且截 10MB（防误配到大文件下载链接炸内存）。"""
        with sf() as s:
            if s.get(KnowledgeBase, kb_id) is None:
                raise AppError("error.kb_not_found", "知识库不存在: {id}", status=404, id=kb_id)
        content, filename = _fetch_url(body.url)
        upload_dir = cfg.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / f"{uuid.uuid4()}-{filename}"
        dest.write_bytes(content)
        bg.add_task(_ingest_batch, kb_id, [(dest, filename)], "auto")
        return {"accepted": [filename], "url": body.url}

    @router.post("/kb/{kb_id}/import-feishu",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def import_feishu(kb_id: str, body: FeishuImportBody, bg: BackgroundTasks):
        """飞书知识库导入（连接器一期）：wiki 节点链接=导该子树，
        space_id=导整个空间。每个 wiki 文档一个 .md（wiki 路径注入标题链，
        heading_path 保留完整层级），转换后走既有摄取管道。
        凭据未配置返回 409——前端据此在导入对话框里就地引导输入。"""
        from urllib.parse import urlparse

        from kbase import feishu
        with sf() as s:
            if s.get(KnowledgeBase, kb_id) is None:
                raise AppError("error.kb_not_found", "知识库不存在: {id}", status=404, id=kb_id)
        app_id, app_secret = feishu.get_credentials(sf)
        if not (app_id and app_secret):
            raise AppError("error.feishu_creds_missing",
                           "未配置飞书应用凭据（app_id/app_secret）", status=409)

        source = body.source.strip()
        try:
            token = feishu._get_token(app_id, app_secret)
            if source.startswith("http"):
                # wiki 节点链接：/wiki/<node_token> → 定位空间与子树根
                seg = [p for p in urlparse(source).path.split("/") if p]
                if "wiki" not in seg:
                    raise AppError("error.not_wiki_link",
                                   "不是飞书 wiki 链接（路径需含 /wiki/）", status=422)
                node_token = seg[seg.index("wiki") + 1]
                node = feishu.resolve_node(token, node_token)
                space_id = str(node.get("space_id"))
                root_title = node.get("title") or "未命名"
                docs = feishu.collect_wiki_docs(
                    token, space_id, root_node_token=node.get("node_token"),
                    root_path=[root_title])
                # 子树根自身若是 docx 也纳入
                if node.get("obj_type") == "docx" and node.get("obj_token"):
                    docs.insert(0, {"obj_token": node["obj_token"],
                                    "title": root_title, "path": []})
            else:
                docs = feishu.collect_wiki_docs(token, source)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 —— 飞书侧错误给可读信息
            # 失败落审计：用户看到的 toast 会消失，运维在审计页可回查原始错误
            from kbase.audit import write_audit
            write_audit(sf, actor="feishu-import", action="feishu_import_failed",
                        resource=f"kb_id={kb_id}", detail=str(e)[:400])
            raise HTTPException(502, f"飞书接口调用失败: {e}") from e
        if not docs:
            raise AppError("error.no_importable_docs",
                           "该范围内没有可导入的文档（仅支持新版文档 docx）", status=404)

        upload_dir = cfg.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        staged: list[tuple[Path, str, list]] = []
        accepted = []
        for doc in docs:
            images: list = []
            try:
                markdown = feishu.doc_to_markdown(token, doc, images_out=images)
            except Exception as e:  # noqa: BLE001 —— 单篇失败不拖垮整批
                accepted.append(f"{doc['title']}（拉取失败: {e}）")
                continue
            filename = f"{Path(doc['title']).name or '未命名'}.md"
            dest = upload_dir / f"{uuid.uuid4()}-{filename}"
            dest.write_text(markdown, encoding="utf-8")
            staged.append((dest, filename, images))
            accepted.append(filename)
        bg.add_task(_ingest_feishu_batch, kb_id, staged, token)
        return {"accepted": accepted, "total": len(staged)}

    def _ingest_feishu_batch(kb_id: str, staged: list, feishu_token: str) -> None:
        """飞书批次专用 bg：逐篇摄取拿 doc_id 后，下载该篇图片素材落
        DocumentImage（逻辑在 feishu.save_doc_images，与连接器同步共用）。"""
        import logging as _logging

        from kbase import feishu
        _logger = _logging.getLogger(__name__)
        for path, name, images in staged:
            try:
                doc_id = pipeline.ingest_file(kb_id, path, name, "auto")
            except Exception as e:  # noqa: BLE001 —— 批次隔离，失败已由 pipeline 落库
                _logger.warning("飞书文档 %s 摄取失败: %s", name, e)
                continue
            feishu.save_doc_images(sf, cfg.data_dir, feishu_token, doc_id,
                                   images)

    @router.post("/demo-data", dependencies=[deps.require_editor, deps.audit_mutation])
    def load_demo_data(request: Request, bg: BackgroundTasks):
        """POC 演示数据一键装载（E，全功能版）：主库（制度/表格/FAQ md +
        含插图 docx——演示多模态回答与 caption 级锚定）+ 副库（案例 md——
        顶栏联查即演示多库联合问答）。幂等：主库同名已存在直接返回其 id，
        不重复灌数——演示环境反复点不会攒出一堆重复库。"""
        from kbase.demo_data import (DEMO_DOCS, DEMO_DOCS_KB2, DEMO_KB2_NAME,
                                     DEMO_KB_NAME, demo_docx_bytes)
        actor = getattr(request.state, "actor", None)
        owner_id = actor.get("user_id") if actor else None
        with sf() as s:
            existing = (s.query(KnowledgeBase)
                        .filter(KnowledgeBase.name == DEMO_KB_NAME).first())
            if existing is not None:
                return {"id": existing.id, "name": DEMO_KB_NAME,
                        "created": False, "accepted": [], "kb_ids": [existing.id]}
            kb1 = KnowledgeBase(id=str(uuid.uuid4()), name=DEMO_KB_NAME,
                                owner_id=owner_id)
            kb2 = KnowledgeBase(id=str(uuid.uuid4()), name=DEMO_KB2_NAME,
                                owner_id=owner_id)
            s.add_all([kb1, kb2])
            s.commit()
            kb1_id, kb2_id = kb1.id, kb2.id
        upload_dir = cfg.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)

        def _stage_texts(docs) -> list[tuple[Path, str]]:
            staged = []
            for filename, content in docs:
                dest = upload_dir / f"{uuid.uuid4()}-{filename}"
                dest.write_text(content, encoding="utf-8")
                staged.append((dest, filename))
            return staged

        items1 = _stage_texts(DEMO_DOCS)
        docx_name = "差旅管理办法.docx"
        docx_dest = upload_dir / f"{uuid.uuid4()}-{docx_name}"
        docx_dest.write_bytes(demo_docx_bytes())
        items1.append((docx_dest, docx_name))
        items2 = _stage_texts(DEMO_DOCS_KB2)

        bg.add_task(_ingest_batch, kb1_id, items1, "auto")
        bg.add_task(_ingest_batch, kb2_id, items2, "auto")
        return {"id": kb1_id, "name": DEMO_KB_NAME, "created": True,
                "kb_ids": [kb1_id, kb2_id],
                "accepted": [f for f, _ in DEMO_DOCS] + [docx_name]
                            + [f for f, _ in DEMO_DOCS_KB2]}

    @router.put("/documents/{doc_id}/review",
                dependencies=[deps.require_editor, deps.audit_mutation])
    def review_document(doc_id: str, body: DocumentReview):
        """F 校验确认：管理员核对（可编辑）VLM 识别文本后确认入库——此刻
        才分块向量化。仅 pending_review 状态可执行（409 否则）。"""
        try:
            found = pipeline.approve_document(doc_id, markdown=body.markdown)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        if not found:
            raise AppError("error.doc_not_found", "文档不存在: {id}", status=404, id=doc_id)
        with sf() as s:
            doc = s.get(Document, doc_id)
            return {"id": doc.id, "status": doc.status, "error": doc.error}

    @router.get("/kb/{kb_id}/documents", dependencies=[deps.require_viewer])
    def list_docs(kb_id: str):
        with sf() as s:
            docs = s.query(Document).filter_by(kb_id=kb_id).all()
            return [{"id": d.id, "filename": d.filename, "status": d.status,
                     "error": d.error} for d in docs]

    @router.get("/documents/{doc_id}/content", dependencies=[deps.require_viewer])
    def document_content(doc_id: str):
        with sf() as s:
            doc = s.get(Document, doc_id)
        if doc is None:
            raise AppError("error.doc_not_found", "文档不存在: {id}", status=404, id=doc_id)
        content_path = cfg.data_dir / "files" / doc_id / "content.md"
        if not content_path.exists():
            raise AppError("error.doc_fulltext_not_found", "文档全文不存在: {id}", status=404, id=doc_id)
        return {"doc_id": doc.id, "filename": doc.filename,
                "markdown": content_path.read_text(encoding="utf-8")}

    # 允许内联预览（浏览器窗口内直接渲染）的 MIME 白名单：PDF 走浏览器自带
    # 查看器（支持 #page= 跳页），图片/纯文本直显。刻意排除 text/html——
    # 上传的 HTML 若被内联渲染，其中脚本会在本站源下执行（存储型 XSS）。
    _INLINE_MEDIA_TYPES = {"application/pdf", "text/plain", "text/markdown"}

    def _inline_allowed(media_type: str) -> bool:
        return (media_type in _INLINE_MEDIA_TYPES
                or media_type.startswith("image/"))

    @router.get("/documents/{doc_id}/original", dependencies=[deps.require_viewer])
    def document_original(doc_id: str, disposition: str = "attachment"):
        """获取识别前的原始上传文件（如 .docx/.pdf/扫描图），文件名恢复为
        用户上传时的原名。数据来源是 Document.source_path——上传时落在
        data_dir/uploads/ 的原件（摄取后不删除，重试 OCR 也依赖它）。
        source_path 为空（极老数据）或文件已被清理时如实 404，
        前端据此隐藏/提示，而不是回退到 Markdown 冒充原文。

        disposition=attachment（默认）：浏览器下载，文件名为上传原名；
        disposition=inline：浏览器内联渲染（M5-2 引用定位预览用，PDF 可配合
        URL fragment #page=N 跳页）——仅白名单类型生效，其余强制 attachment。"""
        with sf() as s:
            doc = s.get(Document, doc_id)
        if doc is None:
            raise AppError("error.doc_not_found", "文档不存在: {id}", status=404, id=doc_id)
        if not doc.source_path or not Path(doc.source_path).exists():
            raise AppError("error.original_file_gone",
                           "原始文件已不存在（历史数据未保留原件或已被清理）", status=404)
        media_type = (mimetypes.guess_type(doc.filename)[0]
                      or "application/octet-stream")
        if disposition == "inline" and _inline_allowed(media_type):
            return FileResponse(doc.source_path, media_type=media_type,
                                content_disposition_type="inline",
                                filename=doc.filename)
        return FileResponse(doc.source_path, media_type=media_type,
                            filename=doc.filename)

    @router.delete("/kb/{kb_id}/documents/{doc_id}",
                   dependencies=[deps.require_editor, deps.audit_mutation])
    def delete_document(kb_id: str, doc_id: str):
        # 级联逻辑在 kbase/doc_delete.py（与连接器同步引擎共用单一事实源）
        from kbase.doc_delete import delete_document_cascade
        if not delete_document_cascade(sf, store, keyword_index, cfg.data_dir,
                                       kb_id, doc_id):
            raise AppError("error.doc_not_found", "文档不存在: {id}", status=404, id=doc_id)
        return {"ok": True}

    @router.post("/documents/{doc_id}/retry",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def retry_document(doc_id: str):
        with sf() as s:
            doc = s.get(Document, doc_id)
        if doc is None:
            raise AppError("error.doc_not_found", "文档不存在: {id}", status=404, id=doc_id)
        pipeline.retry_document(doc_id)
        with sf() as s:
            doc = s.get(Document, doc_id)
            return {"id": doc.id, "status": doc.status, "error": doc.error}

    # ---- Chunk 运营管理（M6-1）----

    @router.get("/documents/{doc_id}/images/{filename}",
                dependencies=[deps.require_viewer])
    def get_document_image(doc_id: str, filename: str):
        """回答附图直链（多模态一期）：files/{doc_id}/images/ 下的提取图。
        filename 强制取纯文件名（Path().name），杜绝 ../ 路径穿越。"""
        safe = Path(filename).name
        if safe != filename or not safe:
            raise AppError("error.image_not_found", "图片不存在", status=404)
        img_path = cfg.data_dir / "files" / doc_id / "images" / safe
        if not img_path.is_file():
            raise AppError("error.image_not_found", "图片不存在", status=404)
        media_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        return FileResponse(str(img_path), media_type=media_type)

    @router.get("/documents/{doc_id}/chunks", dependencies=[deps.require_viewer])
    def list_document_chunks(doc_id: str, offset: int = Query(default=0, ge=0),
                             limit: int = Query(default=50, ge=1, le=200),
                             q: str | None = None):
        """分页列出文档的分块（叶子在前）；q 为文本包含过滤（定位坏块用）。"""
        result = chunk_admin.list_chunks(sf, doc_id, offset=offset,
                                         limit=limit, q=q)
        if result is None:
            raise AppError("error.doc_not_found", "文档不存在: {id}", status=404, id=doc_id)
        return result

    @router.put("/chunks/{chunk_id}",
                dependencies=[deps.require_editor, deps.audit_mutation])
    def update_chunk(chunk_id: str, body: ChunkUpdate):
        """启停/编辑一个块。停用=摘出向量与关键词索引（可恢复）；叶子编辑
        =按该库绑定的向量模型重嵌入+重索引；父块编辑仅落库。"""
        result = chunk_admin.update_chunk(
            sf, store, keyword_index, svc.embedder_for_kb, chunk_id,
            enabled=body.enabled, text=body.text)
        if result is None:
            raise AppError("error.chunk_not_found", "分块不存在: {id}", status=404, id=chunk_id)
        return result

    def _retry_ocr_batch(doc_ids: list[str]) -> None:
        """单入口顺序处理：一个 bg task 内部依次重跑，而不是每个文档各挂一个
        task——避免大批量 pending_ocr 同时并发压垮 OCR 后端（D3）。"""
        for doc_id in doc_ids:
            pipeline.retry_document(doc_id)

    @router.post("/kb/{kb_id}/retry-ocr",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def retry_kb_ocr(kb_id: str, bg: BackgroundTasks):
        with sf() as s:
            pending = s.query(Document).filter_by(
                kb_id=kb_id, status="pending_ocr").all()
            ids = [d.id for d in pending]
        bg.add_task(_retry_ocr_batch, ids)
        return {"queued": len(ids)}
