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

from fastapi import BackgroundTasks, HTTPException, UploadFile
from fastapi.responses import FileResponse

from kbase.api.routes import RouteDeps
from kbase.api.schemas import KBConfigBody, KBCreate
from kbase.api.services import Services
from kbase.models import Chunk, Conversation, Document, KnowledgeBase, Message


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf, cfg, store, keyword_index, pipeline = (
        svc.sf, svc.cfg, svc.store, svc.keyword_index, svc.pipeline)

    @router.get("/embedders", dependencies=[deps.require_viewer])
    def list_embedders():
        """建库下拉用：默认向量模型 + cfg.embedders 可选清单（M5-2）。"""
        return svc.embedder_catalog

    @router.post("/kb", dependencies=[deps.require_editor, deps.audit_mutation])
    def create_kb(body: KBCreate):
        # 绑定校验先行：向量模型 id 必须在配置清单内，建完不可改
        #（换模型=向量空间不可比，该库全部向量作废，只能重建）。
        embedder_id = body.embedder or "default"
        if embedder_id not in svc.embedder_ids:
            raise HTTPException(
                422, f"未知的向量模型: {embedder_id}，可选: {sorted(svc.embedder_ids)}")
        config = (json.dumps({"embedder": embedder_id}, ensure_ascii=False)
                  if embedder_id != "default" else None)
        kb = KnowledgeBase(id=str(uuid.uuid4()), name=body.name, config=config)
        with sf() as s:
            s.add(kb)
            s.commit()
        return {"id": kb.id, "name": kb.name, "embedder": embedder_id}

    @router.get("/kb", dependencies=[deps.require_viewer])
    def list_kb():
        with sf() as s:
            return [{"id": k.id, "name": k.name,
                     "config": json.loads(k.config) if k.config else None}
                    for k in s.query(KnowledgeBase).all()]

    @router.delete("/kb/{kb_id}", dependencies=[deps.require_editor, deps.audit_mutation])
    def delete_kb(kb_id: str):
        with sf() as s:
            kb = s.get(KnowledgeBase, kb_id)
            if kb is None:
                raise HTTPException(404, f"知识库不存在: {kb_id}")
            doc_ids = [d.id for d in s.query(Document).filter_by(kb_id=kb_id).all()]
            conv_ids = [c.id for c in s.query(Conversation).filter_by(kb_id=kb_id).all()]
        # 级联顺序：向量集合 → 全文索引 → files 目录 → chunks/documents 行
        # → conversations/messages 行 → kb 行
        store.delete_collection(kb_id)
        if keyword_index is not None:
            keyword_index.delete_kb(kb_id)
        for doc_id in doc_ids:
            shutil.rmtree(cfg.data_dir / "files" / doc_id, ignore_errors=True)
        with sf() as s:
            s.query(Chunk).filter_by(kb_id=kb_id).delete()
            s.query(Document).filter_by(kb_id=kb_id).delete()
            if conv_ids:
                s.query(Message).filter(Message.conv_id.in_(conv_ids)).delete(
                    synchronize_session=False)
            s.query(Conversation).filter_by(kb_id=kb_id).delete()
            kb = s.get(KnowledgeBase, kb_id)
            if kb is not None:
                s.delete(kb)
            s.commit()
        # 二次清理：若行删除期间有摄取在途 upsert 重建了集合，扫掉这批孤儿向量
        store.delete_collection(kb_id)
        return {"ok": True}

    @router.put("/kb/{kb_id}/config", dependencies=[deps.require_editor, deps.audit_mutation])
    def put_kb_config(kb_id: str, body: KBConfigBody):
        with sf() as s:
            kb = s.get(KnowledgeBase, kb_id)
            if kb is None:
                raise HTTPException(404, f"知识库不存在: {kb_id}")
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

    def _ingest_batch(kb_id: str, items: list[tuple[Path, str]]) -> None:
        """单入口 bg task：ThreadPoolExecutor 并行摄取本批次所有文件（D5）。
        map 是惰性迭代器，必须消费完（list()）才会真正拉起所有任务并等待
        结果；executor 用 with 块在函数返回前 shutdown(wait=True)，保证
        TestClient 的同步 BackgroundTasks 语义下，响应返回时全部文件已经
        摄取完毕（否则测试断言文档状态会在 executor 还没跑完时就检查）。"""
        with ThreadPoolExecutor(max_workers=cfg.ingest.workers) as executor:
            list(executor.map(
                lambda item: pipeline.ingest_file(kb_id, item[0], item[1]),
                items))

    @router.post("/kb/{kb_id}/documents", dependencies=[deps.require_editor, deps.audit_mutation])
    def upload(kb_id: str, files: list[UploadFile], bg: BackgroundTasks):
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
        bg.add_task(_ingest_batch, kb_id, items)
        return {"accepted": accepted}

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
            raise HTTPException(404, f"文档不存在: {doc_id}")
        content_path = cfg.data_dir / "files" / doc_id / "content.md"
        if not content_path.exists():
            raise HTTPException(404, f"文档全文不存在: {doc_id}")
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
            raise HTTPException(404, f"文档不存在: {doc_id}")
        if not doc.source_path or not Path(doc.source_path).exists():
            raise HTTPException(404, "原始文件已不存在（历史数据未保留原件或已被清理）")
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
        with sf() as s:
            doc = s.get(Document, doc_id)
            if doc is None or doc.kb_id != kb_id:
                raise HTTPException(404, f"文档不存在: {doc_id}")
        # 级联顺序：向量 → 全文索引 → chunk 行 → document 行 → files 目录
        store.delete(kb_id, doc_id)
        if keyword_index is not None:
            keyword_index.delete_doc(doc_id)
        with sf() as s:
            s.query(Chunk).filter_by(doc_id=doc_id).delete()
            doc = s.get(Document, doc_id)
            if doc is not None:
                s.delete(doc)
            s.commit()
        shutil.rmtree(cfg.data_dir / "files" / doc_id, ignore_errors=True)
        return {"ok": True}

    @router.post("/documents/{doc_id}/retry",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def retry_document(doc_id: str):
        with sf() as s:
            doc = s.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, f"文档不存在: {doc_id}")
        pipeline.retry_document(doc_id)
        with sf() as s:
            doc = s.get(Document, doc_id)
            return {"id": doc.id, "status": doc.status, "error": doc.error}

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
