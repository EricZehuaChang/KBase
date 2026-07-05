"""TEI（text-embeddings-inference）embedder HTTP 适配器。

真实 TEI `/embed` 契约：POST {"inputs": [str, ...]} -> [[float, ...], ...]
（向量组，与 inputs 顺序一一对应）。动态 batching 由 TEI 服务端负责，本适配器
只按固定大小分批发请求，避免单次请求体过大；不做自适应重试/限流。

错误面：连接失败/超时/非 2xx 一律包成 RuntimeError 并带上 endpoint，让摄取/
查询链路如实失败，而不是悄悄退化（embedder 与 reranker 不同，没有"降级"这一说——
向量拿不到，检索/摄取就是失败的）。
"""
import httpx

from kbase.plugins.registry import registry

_BATCH_SIZE = 64


@registry.register("embedder", "tei")
class TEIEmbedder:
    def __init__(self, endpoint: str, timeout: float = 30,
                 transport: httpx.BaseTransport | None = None):
        self._endpoint = endpoint.rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._dimension: int | None = None   # 惰性探测，首次访问 dimension 时才请求

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            vec = self._embed_batch(["test"])[0]
            self._dimension = len(vec)
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), _BATCH_SIZE):
            out.extend(self._embed_batch(texts[i:i + _BATCH_SIZE]))
        return out

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            resp = self._client.post(f"{self._endpoint}/embed",
                                     json={"inputs": texts})
            resp.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise RuntimeError(
                f"TEI embedder 服务不可达（{self._endpoint}）: {e}") from e
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"TEI embedder 服务返回错误（{self._endpoint}）: "
                f"{e.response.status_code}") from e
        return resp.json()
