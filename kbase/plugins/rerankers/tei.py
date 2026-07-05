"""TEI（text-embeddings-inference）reranker HTTP 适配器。

真实 TEI `/rerank` 契约：POST {"query": str, "texts": [str, ...]}
-> [{"index": int, "score": float}, ...]，响应项通常按 score 降序排列
（不保证与 texts 输入顺序一致）。Retriever 按 candidate_ids 顺序消费
rerank() 的返回值（见 kbase/rag/retriever.py 的
`zip(candidate_ids, scores)`），因此本适配器必须用响应里的 index 把分数
映射回原始 texts 顺序，再返回。

错误面：不可达/非 2xx 抛 RuntimeError。create_app 里 reranker 的*构造*失败
有既有降级路径（捕获异常后 reranker=None，标记 rerank_degraded，见
kbase/api/main.py 中 `except Exception as e` 那段）；但这里说的是*运行时*
rerank() 调用失败——Retriever.retrieve() 在 `self._reranker.rerank(...)`
外层没有 try/except（kbase/rag/retriever.py 约第 124-129 行），异常会一路
冒泡穿出 run_in_threadpool，最终在 /api/kb/{id}/query 与 /api/kb/{id}/search
两个路由里都没有针对性捕获（main.py 里 `_run_query`/`search` 直接
`await run_in_threadpool(retriever.retrieve, ...)`，无 try/except），
所以 TEI 服务在查询期间掉线会变成未处理异常 -> FastAPI 默认 500，而不是
优雅降级为不重排。此为既有行为，本任务不改变它（已按任务要求核实、如实记录，
不修改 Retriever/main.py 的这段逻辑）。
"""
import httpx

from kbase.plugins.registry import registry


@registry.register("reranker", "tei")
class TEIReranker:
    def __init__(self, endpoint: str, timeout: float = 30,
                 transport: httpx.BaseTransport | None = None):
        self._endpoint = endpoint.rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        try:
            resp = self._client.post(f"{self._endpoint}/rerank",
                                     json={"query": query, "texts": texts})
            resp.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise RuntimeError(
                f"TEI reranker 服务不可达（{self._endpoint}）: {e}") from e
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"TEI reranker 服务返回错误（{self._endpoint}）: "
                f"{e.response.status_code}") from e
        scores = [0.0] * len(texts)
        for item in resp.json():
            scores[item["index"]] = item["score"]
        return scores
