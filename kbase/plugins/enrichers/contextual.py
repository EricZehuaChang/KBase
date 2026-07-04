"""上下文增强：LLM 为每个叶子块生成一句全文定位说明，存入 meta["enrich_context"]。
单块失败静默跳过（该块回退为无增强），不向外抛异常。"""
import asyncio

from kbase.plugins.base import ChunkData
from kbase.plugins.registry import registry

_PROMPT = (
    "以下是文档《{doc_name}》的全文（可能截断）：\n{doc_head}\n\n"
    "请用一句话（30字内）说明下面这个片段在全文中的位置与主题，直接输出该句，"
    "不要任何前缀：\n{chunk}"
)


@registry.register("enricher", "contextual")
class ContextualEnricher:
    def __init__(self, llm, max_doc_chars: int = 6000, concurrency: int = 4):
        self._llm = llm
        self._max_doc = max_doc_chars
        self._concurrency = concurrency

    def enrich(self, doc_name, markdown, leaves):
        # asyncio.run 要求当前线程没有运行中的事件循环——调用方为
        # ingest_file（FastAPI BackgroundTasks 工作线程，无 running loop）
        # 或测试（同样无 running loop），因此这里可以直接用。若未来有调用方
        # 已在协程/事件循环内调用本方法，asyncio.run 会抛
        # "asyncio.run() cannot be called from a running event loop"——
        # 届时需要换成 loop.run_until_complete 或把 enrich 变为 async 接口。
        return asyncio.run(self._enrich_async(doc_name, markdown, leaves))

    async def _enrich_async(self, doc_name, markdown, leaves):
        sem = asyncio.Semaphore(self._concurrency)
        doc_head = markdown[: self._max_doc]

        async def one(leaf: ChunkData):
            async with sem:
                try:
                    ctx = await self._llm.complete([{
                        "role": "user",
                        "content": _PROMPT.format(doc_name=doc_name,
                                                  doc_head=doc_head,
                                                  chunk=leaf.text)}])
                    if ctx.strip():
                        leaf.meta["enrich_context"] = ctx.strip()
                except Exception:  # noqa: BLE001 —— 单块回退，不阻塞整批摄取
                    pass

        await asyncio.gather(*(one(leaf) for leaf in leaves))
        return leaves
