"""延迟解析包装器：把"构造需要密钥的 LLM 依赖"从应用启动推迟到首次真正调用。

两个包装器共同的动机：ContextualEnricher / QueryRewriter 都需要一个 LLM 实例，
而构造 LLM（openai-compat）要读 provider 的 api_key 环境变量——若启动时急切
创建，会让根本用不到该功能的部署也被迫配齐密钥才能启动。"""
import logging

logger = logging.getLogger(__name__)


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
                logger.warning("Enricher 初始化失败，本次摄取跳过上下文增强: %s", e)
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

    async def rewrite(self, question: str, history: list[dict],
                      mode: str | None = None):
        from kbase.rag.rewriter import RewriteResult
        if self._resolve_failed:
            return RewriteResult(query=question, triggered=False, rewritten=False)
        if self._resolved is None:
            try:
                self._resolved = self._factory()
            except Exception as e:  # noqa: BLE001 —— 解析失败不阻塞查询
                self._resolve_failed = True
                logger.warning("QueryRewriter 初始化失败，本次查询跳过改写: %s", e)
                return RewriteResult(query=question, triggered=False, rewritten=False)
        return await self._resolved.rewrite(question, history, mode=mode)
