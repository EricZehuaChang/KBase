"""OpenAI 兼容 /embeddings 端点适配器：DashScope/OpenAI/硅基流动等云端向量模型。

契约：POST {base_url}/embeddings，JSON {"model": ..., "input": [str, ...]}，
Bearer 鉴权；响应 {"data": [{"index": n, "embedding": [...]}, ...]}——按 index
回排序（OpenAI 规范不保证 data 顺序与 input 一致）。

批大小默认 10：DashScope text-embedding-v3 的 input 数组上限是 10，取各家
兼容端点的最小公约数；OpenAI 官方端点支持更大批量，可通过 batch_size 调大。

错误面与 TEI embedder 同语义：embedder 没有"降级"一说——向量拿不到，
摄取/检索就是失败的；连接失败/超时/非 2xx 一律包成 RuntimeError 带上
base_url，让链路如实失败而不是悄悄退化。
"""
import os

import httpx

from kbase.plugins.registry import registry


@registry.register("embedder", "openai-embed")
class OpenAICompatEmbedder:
    def __init__(self, base_url: str, model: str, api_key_env: str = "",
                 api_key: str | None = None, batch_size: int = 10,
                 # 默认 60s：部分网络环境下到云端点的首次 TLS 握手就要 5~8s
                 #（本机到 DashScope 实测 7.4s），30s 在批量摄取时会被偶发
                 # 慢握手打穿——embedder 无降级语义，超时=摄取失败，宁可放宽。
                 timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        # 密钥解析顺序与 openai_compat LLM 一致：直传 > 环境变量 > 报错。
        key = api_key or (os.environ.get(api_key_env) if api_key_env else None)
        if not key:
            raise RuntimeError(
                f"embedding 服务未配置密钥：api_key 为空且环境变量 "
                f"{api_key_env or '(未指定)'} 未设置（base_url={base_url}）")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._batch_size = max(1, batch_size)
        self._client = httpx.Client(
            timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {key}"})
        self._dimension: int | None = None   # 惰性探测，首次访问才请求

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            self._dimension = len(self._embed_batch(["test"])[0])
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            out.extend(self._embed_batch(texts[i:i + self._batch_size]))
        return out

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            resp = self._client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": texts})
            resp.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise RuntimeError(
                f"embedding 服务不可达（{self._base_url}）: {e}") from e
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"embedding 服务返回错误（{self._base_url}）: "
                f"{e.response.status_code}") from e
        data = resp.json()["data"]
        # 按 index 回排序：规范不保证 data 与 input 顺序一致
        ordered = sorted(data, key=lambda d: d["index"])
        return [d["embedding"] for d in ordered]
