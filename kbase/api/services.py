"""服务装配组合根：按配置构建核心组件（插件、pipeline、retriever、LLM 缓存）。

create_app（kbase/api/main.py）只负责 HTTP 层的装配（鉴权、路由、静态资源）；
所有「按配置决定用哪个实现、怎么接线」的决策集中在 build_services()——各路由
领域模块（kbase/api/routes/）通过 Services 数据类拿到自己需要的组件，不再靠
一个千行闭包共享状态。

测试注入语义（与 create_app 的参数一一对应，详见 create_app docstring）：
embedder/store/llms/reranker/enricher/ocr_backend/rewriter 均支持
None=按配置加载（生产默认）/ False=显式关闭（部分参数的测试哨兵）/
传实例=直接注入（测试 fake）。"""
import logging
from dataclasses import dataclass
from typing import Callable

from kbase import providers_store
from kbase.api.lazy import LazyEnricher, LazyRewriter
from kbase.config import AppConfig, load_config, resolve_db_url
from kbase.db import make_session_factory
from kbase.index.factory import make_keyword_index
from kbase.ingest.pipeline import IngestPipeline
from kbase.plugins.registry import registry
from kbase.rag.retriever import Retriever
from kbase.rag.rewriter import QueryRewriter

logger = logging.getLogger(__name__)


def _load_builtin_plugins():
    """import 触发注册。新增插件实现时在此登记。"""
    import kbase.plugins.chunkers.structure      # noqa: F401
    import kbase.plugins.vectorstores.chroma_store  # noqa: F401
    import kbase.plugins.vectorstores.qdrant_store  # noqa: F401
    import kbase.plugins.llm.openai_compat       # noqa: F401


@dataclass
class Services:
    """create_app 与各路由域共享的核心服务（原 create_app 闭包捕获的变量）。

    get_llm / invalidate_llm_cache 是一对闭包：LLM 实例缓存（含测试注入的
    fake）由 build_services 内部持有，路由模块只通过这两个函数访问。"""
    cfg: AppConfig
    sf: Callable                     # SQLAlchemy sessionmaker
    embedder: object
    store: object
    keyword_index: object | None
    pipeline: IngestPipeline
    retriever: Retriever
    gen_min_score: float
    rerank_degraded: bool
    rewriter: object                 # LazyRewriter / QueryRewriter / 注入实例
    get_llm: Callable                # (name: str | None) -> LLM
    invalidate_llm_cache: Callable   # (name: str) -> None
    test_llm: object | None          # 测试注入的 active llm（无注入时 None）


def build_services(config_path, *, embedder=None, store=None,
                   llms: dict | None = None, reranker=None,
                   enricher=None, ocr_backend=None, rewriter=None) -> Services:
    _load_builtin_plugins()
    cfg = load_config(config_path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    sf = make_session_factory(resolve_db_url(cfg))
    providers_store.seed_from_config(sf, cfg)   # providers 表为空时才导入 YAML，之后 DB 为唯一真源

    if embedder is None:   # 测试注入 FakeEmbedder，生产走配置
        if cfg.embedder.name == "tei":
            import kbase.plugins.embedders.tei  # noqa: F401
            if not cfg.embedder.endpoint:
                raise ValueError("embedder.name=tei 但未配置 embedder.endpoint")
            embedder = registry.create("embedder", "tei",
                                       endpoint=cfg.embedder.endpoint)
        else:
            # bge_local 依赖 local-embed extra 且加载慢，仅在真正需要时 import 注册
            import kbase.plugins.embedders.bge_local  # noqa: F401
            embedder = registry.create("embedder", cfg.embedder.name,
                                       model=cfg.embedder.model)
    if store is None:
        if cfg.vectorstore.name == "qdrant":
            if not cfg.vectorstore.endpoint:
                raise ValueError("vectorstore.name=qdrant 但未配置 vectorstore.endpoint")
            store = registry.create("vectorstore", "qdrant",
                                    endpoint=cfg.vectorstore.endpoint,
                                    api_key=cfg.vectorstore.api_key)
        else:
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
        with sf() as _s:
            _dialect = _s.get_bind().dialect.name
        keyword_index = make_keyword_index(sf, dialect=_dialect)

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

    def invalidate_llm_cache(name: str) -> None:
        """PUT/DELETE provider 后使该 name 的缓存失效，下次 get_llm 重新按 DB 最新定义创建。
        注意：测试注入的 llm（llms 参数预置进缓存）若被同名 PUT/DELETE，也会被这里清掉——
        这是预期行为：既然通过设置 API 显式改了该 provider，就不应再套用旧的注入实例。"""
        _llm_cache.pop(name, None)

    if enricher is None:
        # 生产路径：延迟解析真实 ContextualEnricher，避免没有任何 kb 启用
        # enrich 时也强制要求 enrich provider 的密钥就绪（见 LazyEnricher 文档）。
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
            if cfg.retrieval.rerank.name == "tei":
                import kbase.plugins.rerankers.tei  # noqa: F401
                if not cfg.retrieval.rerank.endpoint:
                    raise ValueError("rerank.name=tei 但未配置 rerank.endpoint")
                reranker = registry.create("reranker", "tei",
                                           endpoint=cfg.retrieval.rerank.endpoint)
            else:
                import kbase.plugins.rerankers.bge_local  # noqa: F401
                reranker = registry.create("reranker", "bge-local",
                                           model=cfg.retrieval.rerank.model)
        except Exception as e:  # noqa: BLE001 —— 模型加载失败降级不重排
            reranker = None
            rerank_degraded = True
            logger.warning("重排模型加载失败，已降级: %s", e)

    retriever = Retriever(sf, embedder, store, keyword_index=keyword_index,
                          reranker=reranker,
                          candidates=cfg.retrieval.candidates,
                          rrf_k=cfg.retrieval.rrf_k,
                          max_parent_chars=cfg.retrieval.max_parent_chars,
                          max_concurrency=cfg.retrieval.rerank.max_concurrency)
    gen_min_score = (cfg.retrieval.min_score_rerank if retriever.rerank_active
                     else cfg.retrieval.min_score_dense)

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

    return Services(
        cfg=cfg, sf=sf, embedder=embedder, store=store,
        keyword_index=keyword_index, pipeline=pipeline, retriever=retriever,
        gen_min_score=gen_min_score, rerank_degraded=rerank_degraded,
        rewriter=rewriter, get_llm=get_llm,
        invalidate_llm_cache=invalidate_llm_cache,
        # 测试注入路径：暴露被注入的 active llm 实例，便于测试直接断言其记录的
        # last_messages（如 FakeLLM）；生产路径未注入任何 llm 时为 None。
        test_llm=(llms or {}).get(cfg.llm.active),
    )
