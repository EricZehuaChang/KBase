"""HTTP 编排层：只做参数校验与调度，业务逻辑在 ingest/rag 模块。"""
import json
import logging
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from kbase.config import load_config
from kbase.db import make_session_factory
from kbase.index.keyword import KeywordIndex
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import Document, KnowledgeBase
from kbase.plugins.registry import registry
from kbase.rag.generator import Generator
from kbase.rag.retriever import Retriever


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


class KBCreate(BaseModel):
    name: str


class QueryBody(BaseModel):
    question: str
    provider: str | None = None     # 不传用配置里的 active —— 模型对比入口
    top_k: int = 5


def create_app(config_path="config/kbase.yaml", *, embedder=None,
               store=None, llms: dict | None = None, reranker=None,
               enricher=None, ocr_backend=None) -> FastAPI:
    """reranker: None=按配置加载（生产默认，失败自动降级）；
    False=显式关闭（测试用哨兵，保持既有分数语义不变）；
    传实例=直接注入（测试用 fake reranker）。
    enricher: None=生产默认，包一层 LazyEnricher 延迟到首次摄取时才解析真实
    LLM（避免未启用 enrich 的部署也要求配好密钥）；
    False=显式关闭（测试哨兵，pipeline 不做任何增强）；
    传实例=直接注入（测试用真实 ContextualEnricher 或 fake，跳过 Lazy 包装）。
    ocr_backend: None=按配置加载（cfg.ocr.enabled 为真时创建 monkey-http 后端，
    生产默认；未启用则不装 OCR，扫描件/图片直接判 failed）；
    传实例=直接注入（测试用 FakeOCR，跳过配置读取与真实网络依赖）。"""
    _load_builtin_plugins()
    cfg = load_config(config_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    sf = make_session_factory(f"sqlite:///{cfg.data_dir}/kbase.sqlite")

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
                          rrf_k=cfg.retrieval.rrf_k)
    gen_min_score = (cfg.retrieval.min_score_rerank if retriever.rerank_active
                     else cfg.retrieval.min_score_dense)

    _llm_cache: dict = dict(llms or {})

    def get_llm(name: str | None):
        pname = name or cfg.llm.active
        if pname not in _llm_cache:      # 懒创建：请求时在应用事件循环内构建，
            p = cfg.get_provider(pname)  # 未配密钥的 provider 不影响启动
            _llm_cache[pname] = registry.create(
                "llm", "openai-compat", base_url=p.base_url,
                api_key_env=p.api_key_env, model=p.model,
                max_concurrency=p.max_concurrency, params=p.params)
        return _llm_cache[pname]

    app = FastAPI(title="KBase")

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
        return {"active": cfg.llm.active,
                "providers": [p.name for p in cfg.llm.providers]}

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
            return [{"id": k.id, "name": k.name}
                    for k in s.query(KnowledgeBase).all()]

    @app.post("/api/kb/{kb_id}/documents")
    def upload(kb_id: str, files: list[UploadFile], bg: BackgroundTasks):
        upload_dir = cfg.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        accepted = []
        for f in files:
            safe_name = Path(f.filename or "unnamed").name   # 去除路径分隔符，防穿越；兜底 None
            dest = upload_dir / f"{uuid.uuid4()}-{safe_name}"
            dest.write_bytes(f.file.read())
            bg.add_task(pipeline.ingest_file, kb_id, dest, safe_name)
            accepted.append(safe_name)
        return {"accepted": accepted}

    @app.get("/api/kb/{kb_id}/documents")
    def list_docs(kb_id: str):
        with sf() as s:
            docs = s.query(Document).filter_by(kb_id=kb_id).all()
            return [{"id": d.id, "filename": d.filename, "status": d.status,
                     "error": d.error} for d in docs]

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

    @app.post("/api/kb/{kb_id}/retry-ocr")
    def retry_kb_ocr(kb_id: str, bg: BackgroundTasks):
        with sf() as s:
            pending = s.query(Document).filter_by(
                kb_id=kb_id, status="pending_ocr").all()
            ids = [d.id for d in pending]
        for doc_id in ids:
            bg.add_task(pipeline.retry_document, doc_id)
        return {"retrying": ids}

    @app.post("/api/kb/{kb_id}/query")
    async def query(kb_id: str, body: QueryBody):
        try:
            llm = get_llm(body.provider)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:      # 环境变量未设置等初始化失败：给前端可读信息
            raise HTTPException(503, str(e)) from e
        # 检索（含向量化）是同步 CPU/IO 混合操作，进线程池避免阻塞事件循环
        blocks = await run_in_threadpool(
            retriever.retrieve, kb_id, body.question, body.top_k)
        gen = Generator(llm, min_score=gen_min_score)
        # 关键契约：usable_blocks 只算一次，citations 与 answer_stream 用同一份列表，
        # 保证引用编号与答案中的 [n] 标记对齐（拒答时 citations 为空列表）
        usable = gen.usable_blocks(blocks)

        async def events():
            yield {"event": "citations",
                   "data": json.dumps(gen.citations(usable), ensure_ascii=False)}
            async for piece in gen.answer_stream(body.question, usable):
                yield {"event": "token", "data": piece}
            yield {"event": "done", "data": ""}

        return EventSourceResponse(events())

    web_dir = Path(__file__).resolve().parents[2] / "web"
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    return app
