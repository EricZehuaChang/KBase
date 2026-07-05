"""HTTP 编排层：只做参数校验与调度，业务逻辑在 ingest/rag 模块。"""
import asyncio
import json
import logging
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, StrictInt, model_validator
from sse_starlette.sse import EventSourceResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope


class SPAStaticFiles(StaticFiles):
    """SPA 回退路由：真实静态资源正常返回；404 时（如 /kb 这类前端路由深链接）
    回退到 index.html，交由前端 router 接管——标准 FastAPI SPA 托管模式。
    该 mount 挂在 "/"，未匹配到任何 API 路由的 /api/* 请求也会落到这里——
    必须显式排除，否则 /api/nonexistent 会被错误地回退成 200 的 index.html
    而不是 404，掩盖真正的路由错误。
    注意：StaticFiles.get_response 未命中时是 raise HTTPException(404)，
    不是返回 404 响应，所以要 except 而不是检查返回值的状态码。"""

    async def get_response(self, path: str, scope: Scope) -> Response:
        # get_path() 用 os.path.join 拼出 path，Windows 上分隔符是反斜杠，
        # 用 PurePosixPath 统一成 "/" 再判断前缀，避免平台差异漏判。
        normalized = path.replace("\\", "/")
        if normalized == "api" or normalized.startswith("api/") or normalized == "healthz":
            return await super().get_response(path, scope)
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)

from kbase import conversations as conv_store
from kbase import providers_store
from kbase.config import load_config
from kbase.db import make_session_factory
from kbase.index.keyword import KeywordIndex
from kbase.ingest.pipeline import IngestPipeline
from kbase.jobs.digest import build_digest_steps
from kbase.jobs.export_docx import markdown_to_docx
from kbase.jobs.proposal import build_proposal_steps, generate_outline
from kbase.jobs.runner import run_job
from kbase.jobs.store import create_job, get_job, list_jobs
from kbase.models import Chunk, Conversation, Document, KnowledgeBase, Message
from kbase.plugins.registry import registry
from kbase.rag.generator import Generator
from kbase.rag.retriever import Retriever
from kbase.rag.rewriter import QueryRewriter


def _load_builtin_plugins():
    """import 触发注册。新增插件实现时在此登记。"""
    import kbase.plugins.chunkers.structure      # noqa: F401
    import kbase.plugins.vectorstores.chroma_store  # noqa: F401
    import kbase.plugins.llm.openai_compat       # noqa: F401


class LazyEnricher:
    """包一层，把"是否需要 enrich LLM"从"应用启动"推迟到"第一次真正调用 enrich"。

    动机：ContextualEnricher 需要一个 LLM 实例，但构造 LLM（openai-compat）
    要读 provider 的 api_key 环境变量——如果启动时就急切创建，会让「没有任何
    kb 开启 enrich」的部署也强制要求配好 enrich provider 的密钥。而是否有 kb
    真正启用 enrich 只有摄取时才知道（kb 级 config JSON），所以用一个可调用
    工厂延迟到首次 enrich() 调用时再解析真实的 ContextualEnricher。
    解析失败（如密钥缺失/provider 不存在）时记录 warning 并原样返回 leaves
    （等价于未增强），不让摄取失败。
    """

    def __init__(self, factory):
        self._factory = factory
        self._resolved = None
        self._resolve_failed = False

    def enrich(self, doc_name, markdown, leaves):
        if self._resolve_failed:
            return leaves
        if self._resolved is None:
            try:
                self._resolved = self._factory()
            except Exception as e:  # noqa: BLE001 —— 解析失败不阻塞摄取
                self._resolve_failed = True
                logging.getLogger(__name__).warning(
                    "Enricher 初始化失败，本次摄取跳过上下文增强: %s", e)
                return leaves
        return self._resolved.enrich(doc_name, markdown, leaves)


class LazyRewriter:
    """包一层，把"改写用哪个 LLM"从"应用启动"推迟到"第一次真正调用 rewrite"。

    动机同 LazyEnricher：QueryRewriter 需要一个 LLM 实例，但构造 LLM
    （openai-compat）要读 provider 的 api_key 环境变量——如果启动时就急切创建，
    会让「未配置改写 provider 密钥」的部署也无法启动。是否真的需要改写只有
    收到会话查询时才知道，所以用一个可调用工厂延迟到首次 rewrite() 调用时
    再解析真实的 QueryRewriter。解析失败时记录 warning 并原样返回原问题
    （等价于未改写、rewriter=off），不让查询链路失败。
    """

    def __init__(self, factory):
        self._factory = factory
        self._resolved = None
        self._resolve_failed = False

    async def rewrite(self, question: str, history: list[dict]):
        from kbase.rag.rewriter import RewriteResult
        if self._resolve_failed:
            return RewriteResult(query=question, triggered=False, rewritten=False)
        if self._resolved is None:
            try:
                self._resolved = self._factory()
            except Exception as e:  # noqa: BLE001 —— 解析失败不阻塞查询
                self._resolve_failed = True
                logging.getLogger(__name__).warning(
                    "QueryRewriter 初始化失败，本次查询跳过改写: %s", e)
                return RewriteResult(query=question, triggered=False, rewritten=False)
        return await self._resolved.rewrite(question, history)


class KBCreate(BaseModel):
    name: str


class QueryBody(BaseModel):
    question: str
    provider: str | None = None     # 不传用配置里的 active —— 模型对比入口
    top_k: int = 5


class SearchBody(BaseModel):
    query: str
    top_k: int = 5
    debug: bool = False


class ConversationCreate(BaseModel):
    kb_id: str


class EnrichConfigBody(BaseModel):
    enabled: StrictBool


class KBConfigBody(BaseModel):
    """PUT /api/kb/{kb_id}/config 请求体：只接受已知 key，未知 key 由
    model_config extra="forbid" 拒绝（422），避免前端笔误的字段被静默丢弃。"""
    model_config = {"extra": "forbid"}

    chunk_size: StrictInt | None = Field(default=None, ge=64, le=4096)
    chunk_overlap: StrictInt | None = Field(default=None, ge=0, le=512)
    enrich: EnrichConfigBody | None = None

    @model_validator(mode="after")
    def _check_overlap_lt_size(self):
        if (self.chunk_size is not None and self.chunk_overlap is not None
                and self.chunk_overlap >= self.chunk_size):
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        return self


class ProviderCreate(BaseModel):
    name: str
    base_url: str
    api_key_env: str
    model: str
    max_concurrency: int = 4
    params: dict = {}


class ProviderUpdate(BaseModel):
    base_url: str | None = None
    api_key_env: str | None = None
    model: str | None = None
    max_concurrency: int | None = None
    params: dict | None = None


class ActiveProviderBody(BaseModel):
    name: str


class OutlineBody(BaseModel):
    kb_id: str
    topic: str
    requirements: str = ""
    provider: str | None = None


class JobCreate(BaseModel):
    type: str
    kb_id: str
    provider: str | None = None
    params: dict = {}


def create_app(config_path="config/kbase.yaml", *, embedder=None,
               store=None, llms: dict | None = None, reranker=None,
               enricher=None, ocr_backend=None, rewriter=None) -> FastAPI:
    """reranker: None=按配置加载（生产默认，失败自动降级）；
    False=显式关闭（测试用哨兵，保持既有分数语义不变）；
    传实例=直接注入（测试用 fake reranker）。
    enricher: None=生产默认，包一层 LazyEnricher 延迟到首次摄取时才解析真实
    LLM（避免未启用 enrich 的部署也要求配好密钥）；
    False=显式关闭（测试哨兵，pipeline 不做任何增强）；
    传实例=直接注入（测试用真实 ContextualEnricher 或 fake，跳过 Lazy 包装）。
    ocr_backend: None=按配置加载（cfg.ocr.enabled 为真时创建 monkey-http 后端，
    生产默认；未启用则不装 OCR，扫描件/图片直接判 failed）；
    传实例=直接注入（测试用 FakeOCR，跳过配置读取与真实网络依赖）。
    rewriter: None=生产默认，包一层 LazyRewriter 延迟到首次会话查询时才解析
    真实 QueryRewriter（避免未用到改写的部署也要求配好改写 provider 密钥）；
    False=显式关闭（测试哨兵/等价 mode=off，会话查询检索用原文，行为与 M2
    完全一致）；传实例=直接注入（测试用真实 QueryRewriter 搭配 FakeLLM，跳过
    Lazy 包装）。仅会话查询路由（/api/conversations/{id}/query）使用；
    旧的 /api/kb/{id}/query 端点不接入改写，行为字节级不变。"""
    _load_builtin_plugins()
    cfg = load_config(config_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    sf = make_session_factory(f"sqlite:///{cfg.data_dir}/kbase.sqlite")
    providers_store.seed_from_config(sf, cfg)   # providers 表为空时才导入 YAML，之后 DB 为唯一真源

    if embedder is None:   # 测试注入 FakeEmbedder，生产走配置
        # bge_local 依赖 local-embed extra 且加载慢，仅在真正需要时 import 注册
        import kbase.plugins.embedders.bge_local  # noqa: F401
        embedder = registry.create("embedder", cfg.embedder.name,
                                   model=cfg.embedder.model)
    if store is None:
        store = registry.create("vectorstore", cfg.vectorstore.name,
                                persist_dir=str(cfg.data_dir / "chroma"))
    chunker = registry.create("chunker", cfg.chunker.name,
                              chunk_size=cfg.chunker.chunk_size,
                              chunk_overlap=cfg.chunker.chunk_overlap)

    keyword_index = None
    if cfg.retrieval.hybrid:
        # jieba 词典默认懒加载（首次分词时才建词典树），若把首次分词留给
        # 请求期的线程池 worker，多个并发请求可能同时触发构建、产生竞态；
        # 这里在应用启动阶段（单线程）提前 eager 初始化，避免该竞态。
        import jieba
        jieba.initialize()
        keyword_index = KeywordIndex(sf)

    if enricher is None:
        # 生产路径：延迟解析真实 ContextualEnricher，避免没有任何 kb 启用
        # enrich 时也强制要求 enrich provider 的密钥就绪（见 LazyEnricher 文档）。
        # get_llm 在下方定义，这里只是把它捕获进闭包，调用发生在首次 enrich()
        # 时（即已在 get_llm 定义之后），Python 闭包晚绑定，不存在时序问题。
        def _build_enricher():
            import kbase.plugins.enrichers.contextual  # noqa: F401
            llm = get_llm(cfg.enrich.provider)
            return registry.create("enricher", "contextual", llm=llm)

        enricher = LazyEnricher(_build_enricher)
    elif enricher is False:
        enricher = None    # 显式关闭：测试哨兵

    if ocr_backend is None and cfg.ocr.enabled:
        import kbase.plugins.ocr.monkey_http  # noqa: F401
        ocr_backend = registry.create("ocr", cfg.ocr.backend, endpoint=cfg.ocr.endpoint)

    pipeline = IngestPipeline(sf, chunker, embedder, store,
                              files_dir=cfg.data_dir / "files",
                              keyword_index=keyword_index,
                              enricher=enricher,
                              ocr_backend=ocr_backend)

    rerank_degraded = False
    if reranker is False:
        reranker = None                     # 显式关闭：测试哨兵，不走加载/降级逻辑
    elif reranker is None and cfg.retrieval.rerank.enabled:
        try:
            import kbase.plugins.rerankers.bge_local  # noqa: F401
            reranker = registry.create("reranker", "bge-local",
                                       model=cfg.retrieval.rerank.model)
        except Exception as e:  # noqa: BLE001 —— 模型加载失败降级不重排
            reranker = None
            rerank_degraded = True
            logging.getLogger(__name__).warning("重排模型加载失败，已降级: %s", e)

    retriever = Retriever(sf, embedder, store, keyword_index=keyword_index,
                          reranker=reranker,
                          candidates=cfg.retrieval.candidates,
                          rrf_k=cfg.retrieval.rrf_k,
                          max_parent_chars=cfg.retrieval.max_parent_chars)
    gen_min_score = (cfg.retrieval.min_score_rerank if retriever.rerank_active
                     else cfg.retrieval.min_score_dense)

    # 测试注入的 llm（按 name）继续短路 DB 懒创建——预置进缓存后，get_llm
    # 命中缓存直接返回，不会走到下面的 DB provider 查询，保证既有测试不变。
    _llm_cache: dict = dict(llms or {})

    def get_llm(name: str | None):
        """活跃 provider 解析顺序：显式请求参数 > app_settings.active_provider > 报错。"""
        pname = name
        if pname is None:
            pname = providers_store.get_active(sf)
            if pname is None:
                raise KeyError("未设置活跃 provider（app_settings.active_provider 为空）")
        if pname not in _llm_cache:      # 懒创建：请求时在应用事件循环内构建，
            p = providers_store.get_provider_dict(sf, pname)   # 未配密钥的 provider 不影响启动
            if p is None:
                raise KeyError(f"LLM provider 未配置: {pname}")
            _llm_cache[pname] = registry.create(
                "llm", "openai-compat", base_url=p["base_url"],
                api_key_env=p["api_key_env"], model=p["model"],
                max_concurrency=p["max_concurrency"], params=p["params"])
        return _llm_cache[pname]

    def _invalidate_llm_cache(name: str) -> None:
        """PUT/DELETE provider 后使该 name 的缓存失效，下次 get_llm 重新按 DB 最新定义创建。
        注意：测试注入的 llm（llms 参数预置进缓存）若被同名 PUT/DELETE，也会被这里清掉——
        这是预期行为：既然通过设置 API 显式改了该 provider，就不应再套用旧的注入实例。"""
        _llm_cache.pop(name, None)

    if rewriter is None:
        # 生产路径：延迟解析真实 QueryRewriter，避免会话查询链路未真正触发
        # 改写时也强制要求改写 provider 的密钥就绪（见 LazyRewriter 文档）。
        def _build_rewriter():
            llm = get_llm(cfg.retrieval.rewrite.provider)
            return QueryRewriter(llm=llm, mode=cfg.retrieval.rewrite.mode,
                                 max_wait_s=cfg.retrieval.rewrite.max_wait_s)

        rewriter = LazyRewriter(_build_rewriter)
    elif rewriter is False:
        # 显式关闭：测试哨兵/等价 mode=off。mode="off" 的 should_rewrite 永远
        # 短路返回 False，不会触碰 llm，因此不需要 Lazy 包装，可直接构造。
        rewriter = QueryRewriter(llm=None, mode="off")

    app = FastAPI(title="KBase")
    # 测试注入路径：暴露被注入的 active llm 实例，便于测试直接断言其记录的
    # last_messages（如 FakeLLM），而不必依赖内部私有变量；生产路径未注入
    # 任何 llm 时为 None。
    app.state.test_llm = (llms or {}).get(cfg.llm.active)

    @app.get("/healthz")
    def healthz():
        if rerank_degraded:
            reranker_status = "degraded"
        elif retriever.rerank_active:
            reranker_status = "on"
        else:
            reranker_status = "off"
        return {"status": "ok", "embedder": type(embedder).__name__,
                "vectorstore": type(store).__name__,
                "reranker": reranker_status}

    @app.get("/api/providers")
    def providers():
        # 旧端点（Plan B 前旧前端仍用它）：改读 DB，返回结构保持不变
        return {"active": providers_store.get_active(sf),
                "providers": [p["name"] for p in providers_store.list_providers(sf)]}

    @app.get("/api/settings/providers")
    def settings_list_providers():
        return {"active": providers_store.get_active(sf),
                "providers": providers_store.list_providers(sf)}

    @app.post("/api/settings/providers")
    def settings_create_provider(body: ProviderCreate):
        if providers_store.get_provider_dict(sf, body.name) is not None:
            raise HTTPException(409, f"provider 已存在: {body.name}")
        providers_store.create_provider(sf, body.model_dump())
        return {"ok": True}

    @app.put("/api/settings/providers/{name}")
    def settings_update_provider(name: str, body: ProviderUpdate):
        found = providers_store.update_provider(
            sf, name, body.model_dump(exclude_unset=True))
        if not found:
            raise HTTPException(404, f"provider 不存在: {name}")
        _invalidate_llm_cache(name)
        return {"ok": True}

    @app.delete("/api/settings/providers/{name}")
    def settings_delete_provider(name: str):
        if providers_store.get_active(sf) == name:
            raise HTTPException(409, "默认 provider 不可删除，请先切换默认")
        found = providers_store.delete_provider(sf, name)
        if not found:
            raise HTTPException(404, f"provider 不存在: {name}")
        _invalidate_llm_cache(name)
        return {"ok": True}

    @app.put("/api/settings/active-provider")
    def settings_set_active_provider(body: ActiveProviderBody):
        if providers_store.get_provider_dict(sf, body.name) is None:
            raise HTTPException(404, f"provider 不存在: {body.name}")
        providers_store.set_active(sf, body.name)
        return {"ok": True}

    @app.post("/api/settings/providers/{name}/test")
    async def settings_test_provider(name: str):
        if providers_store.get_provider_dict(sf, name) is None:
            raise HTTPException(404, f"provider 不存在: {name}")
        try:
            llm = get_llm(name)
            start = time.perf_counter()
            await asyncio.wait_for(
                llm.complete([{"role": "user", "content": "回复：好"}]), timeout=10.0)
            latency_ms = (time.perf_counter() - start) * 1000
            return {"ok": True, "latency_ms": latency_ms}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "连通性测试超时(10s)"}
        except Exception as e:  # noqa: BLE001 —— 连通性探测，任何失败都回报而非 500
            return {"ok": False, "error": str(e)}

    @app.post("/api/kb")
    def create_kb(body: KBCreate):
        kb = KnowledgeBase(id=str(uuid.uuid4()), name=body.name)
        with sf() as s:
            s.add(kb)
            s.commit()
        return {"id": kb.id, "name": kb.name}

    @app.get("/api/kb")
    def list_kb():
        with sf() as s:
            return [{"id": k.id, "name": k.name,
                     "config": json.loads(k.config) if k.config else None}
                    for k in s.query(KnowledgeBase).all()]

    @app.delete("/api/kb/{kb_id}")
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

    @app.put("/api/kb/{kb_id}/config")
    def put_kb_config(kb_id: str, body: KBConfigBody):
        with sf() as s:
            kb = s.get(KnowledgeBase, kb_id)
            if kb is None:
                raise HTTPException(404, f"知识库不存在: {kb_id}")
            kb.config = json.dumps(body.model_dump(exclude_none=True), ensure_ascii=False)
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

    @app.post("/api/kb/{kb_id}/documents")
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

    @app.get("/api/kb/{kb_id}/documents")
    def list_docs(kb_id: str):
        with sf() as s:
            docs = s.query(Document).filter_by(kb_id=kb_id).all()
            return [{"id": d.id, "filename": d.filename, "status": d.status,
                     "error": d.error} for d in docs]

    @app.get("/api/documents/{doc_id}/content")
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

    @app.delete("/api/kb/{kb_id}/documents/{doc_id}")
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

    @app.post("/api/documents/{doc_id}/retry")
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

    @app.post("/api/kb/{kb_id}/retry-ocr")
    def retry_kb_ocr(kb_id: str, bg: BackgroundTasks):
        with sf() as s:
            pending = s.query(Document).filter_by(
                kb_id=kb_id, status="pending_ocr").all()
            ids = [d.id for d in pending]
        bg.add_task(_retry_ocr_batch, ids)
        return {"queued": len(ids)}

    async def _run_query(kb_id: str, body: QueryBody, *,
                         history: list[dict] | None = None,
                         on_complete=None, retrieval_query: str | None = None):
        """共享检索+生成编排：会话端点与旧的 /api/kb/{id}/query 端点复用同一份
        逻辑，保证事件序列（citations→token*→done）与拒答语义完全一致。

        on_complete(answer_text, citations, provider): 流结束（含客户端中断）
        后调用，用于会话落库；旧端点不传，行为与改造前完全相同。
        retrieval_query: 检索实际使用的问题文本；默认 None 时等同 body.question
        （旧端点 /api/kb/{id}/query 不传，行为字节级不变）。会话端点在触发
        QueryRewrite 时传入改写后的问题——生成（answer_stream）与落库
        （on_complete）仍固定使用 body.question（原文），只有检索这一步换词。
        """
        try:
            llm = get_llm(body.provider)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:      # 环境变量未设置等初始化失败：给前端可读信息
            raise HTTPException(503, str(e)) from e
        query_text = retrieval_query if retrieval_query is not None else body.question
        # 检索（含向量化）是同步 CPU/IO 混合操作，进线程池避免阻塞事件循环
        blocks = await run_in_threadpool(
            retriever.retrieve, kb_id, query_text, body.top_k)
        gen = Generator(llm, min_score=gen_min_score,
                        min_include_score=cfg.retrieval.min_include_score)
        # 关键契约：usable_blocks 只算一次，citations 与 answer_stream 用同一份列表，
        # 保证引用编号与答案中的 [n] 标记对齐（拒答时 citations 为空列表）
        usable = gen.usable_blocks(blocks)
        citations = gen.citations(usable)

        async def events():
            pieces: list[str] = []
            try:
                yield {"event": "citations",
                       "data": json.dumps(citations, ensure_ascii=False)}
                async for piece in gen.answer_stream(body.question, usable, history):
                    pieces.append(piece)
                    yield {"event": "token", "data": piece}
                yield {"event": "done", "data": ""}
            finally:
                # 客户端中断（GeneratorExit）时也执行：已生成的部分答案（可能为空）
                # 连同引用一并落库，拒答场景（usable 为空）同样落库。
                if on_complete is not None:
                    on_complete("".join(pieces), citations,
                               getattr(llm, "model", body.provider or cfg.llm.active))

        return EventSourceResponse(events())

    @app.post("/api/kb/{kb_id}/query")
    async def query(kb_id: str, body: QueryBody):
        return await _run_query(kb_id, body)

    @app.post("/api/kb/{kb_id}/search")
    async def search(kb_id: str, body: SearchBody):
        """检索调试端点：debug=False 只返回 blocks（不含 trace key，向后兼容展示用途）；
        debug=True 额外返回各阶段 trace（dense/keyword/fused[/reranked]），用于排查召回质量。
        检索是同步 CPU/IO 混合操作，进线程池避免阻塞事件循环。"""
        result = await run_in_threadpool(
            retriever.retrieve, kb_id, body.query, body.top_k, body.debug)
        if body.debug:
            return {"blocks": [asdict(b) for b in result.blocks],
                    "trace": result.trace}
        return {"blocks": [asdict(b) for b in result]}

    @app.post("/api/conversations")
    def create_conversation(body: ConversationCreate):
        return conv_store.create_conversation(sf, body.kb_id)

    @app.get("/api/conversations")
    def list_conversations(kb_id: str | None = None,
                           limit: int = Query(default=30, ge=1, le=100),
                           offset: int = Query(default=0, ge=0)):
        return conv_store.list_conversations(sf, kb_id, limit=limit, offset=offset)

    @app.get("/api/conversations/{conv_id}/messages")
    def list_conversation_messages(conv_id: str):
        return conv_store.list_messages(sf, conv_id)

    @app.post("/api/conversations/{conv_id}/query")
    async def query_conversation(conv_id: str, body: QueryBody):
        with sf() as s:
            conv = s.get(Conversation, conv_id)
        if conv is None:
            raise HTTPException(404, f"会话不存在: {conv_id}")
        history = conv_store.build_history(sf, conv_id)
        # 改写只影响检索输入；生成与落库仍固定使用 body.question（原文），
        # 见 _run_query 的 retrieval_query 参数文档。
        rewrite_res = await rewriter.rewrite(body.question, history)

        def _persist(answer: str, citations: list[dict], provider: str):
            conv_store.append_round(sf, conv_id, body.question, answer,
                                    citations, provider)

        return await _run_query(conv.kb_id, body, history=history,
                               retrieval_query=rewrite_res.query,
                               on_complete=_persist)

    @app.post("/api/proposals/outline")
    async def proposals_outline(body: OutlineBody):
        try:
            llm = get_llm(body.provider)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(503, str(e)) from e
        try:
            return await generate_outline(retriever, llm, body.kb_id, body.topic,
                                          body.requirements)
        except ValueError as e:
            raise HTTPException(502, str(e)) from e

    @app.post("/api/jobs")
    def create_job_endpoint(body: JobCreate, bg: BackgroundTasks):
        if body.type not in ("proposal", "digest"):
            raise HTTPException(422, f"未知的 job type: {body.type}")
        if body.type == "proposal":
            if "topic" not in body.params or "outline" not in body.params:
                raise HTTPException(422, "proposal job 缺少必需参数：topic/outline")
        with sf() as s:
            kb = s.get(KnowledgeBase, body.kb_id)
        if kb is None:
            raise HTTPException(404, f"知识库不存在: {body.kb_id}")
        try:
            llm = get_llm(body.provider)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(503, str(e)) from e

        job = create_job(sf, kb_id=body.kb_id, type=body.type,
                         params=body.params, provider=body.provider)
        jobs_dir = cfg.data_dir / "jobs"

        if body.type == "proposal":
            steps = build_proposal_steps(
                sf, retriever, llm, kb_id=body.kb_id,
                topic=body.params["topic"], outline=body.params["outline"],
                job_id=job["id"], jobs_dir=jobs_dir)
        else:
            steps = build_digest_steps(
                sf, llm, kb_id=body.kb_id, doc_ids=body.params.get("doc_ids"),
                job_id=job["id"], files_dir=cfg.data_dir / "files",
                jobs_dir=jobs_dir, kb_name=kb.name)

        bg.add_task(run_job, sf, job["id"], steps)
        return {"id": job["id"]}

    @app.get("/api/jobs")
    def jobs_list(kb_id: str):
        return list_jobs(sf, kb_id)

    @app.get("/api/jobs/{job_id}")
    def jobs_detail(job_id: str):
        job = get_job(sf, job_id)
        if job is None:
            raise HTTPException(404, f"job 不存在: {job_id}")
        return job

    _ARTIFACT_FILENAME = {"proposal": "方案.docx", "digest": "汇编.docx"}
    _DOCX_MEDIA_TYPE = ("application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document")

    @app.get("/api/jobs/{job_id}/artifact")
    def jobs_artifact(job_id: str, format: str = "md"):
        job = get_job(sf, job_id)
        if job is None:
            raise HTTPException(404, f"job 不存在: {job_id}")
        if job["status"] not in ("done", "done_with_errors"):
            raise HTTPException(409, f"job 尚未完成: status={job['status']}")
        if not job["artifact_path"]:
            raise HTTPException(404, "产物不存在")
        md_path = Path(job["artifact_path"])
        if not md_path.exists():
            raise HTTPException(404, "产物文件不存在")

        if format == "md":
            return FileResponse(md_path, media_type="text/markdown",
                                filename="artifact.md")

        # docx：首次请求时按需转换并缓存在 md 旁边
        docx_path = md_path.with_suffix(".docx")
        if not docx_path.exists():
            markdown_to_docx(md_path.read_text(encoding="utf-8"), docx_path)
        download_name = _ARTIFACT_FILENAME.get(job["type"], "artifact.docx")
        return FileResponse(docx_path, media_type=_DOCX_MEDIA_TYPE,
                            filename=download_name)

    web_dir = Path(__file__).resolve().parents[2] / "web"
    if web_dir.exists():
        app.mount("/", SPAStaticFiles(directory=str(web_dir), html=True), name="web")
    return app
