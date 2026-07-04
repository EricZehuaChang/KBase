"""插件契约。内核代码只允许 import 本文件，不得 import 具体实现。

runtime_checkable 只校验方法存在，不校验签名。
"""
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass
class ChunkData:
    """分块结果。parent_id 为 None 表示父块（章节级），否则为叶子块。"""
    id: str
    text: str
    heading_path: str            # 如 "文件名 > 第三章 > 第十二条"
    parent_id: str | None = None
    prev_id: str | None = None
    next_id: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Hit:
    chunk_id: str
    score: float
    meta: dict = field(default_factory=dict)


@runtime_checkable
class Embedder(Protocol):
    dimension: int
    def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class VectorStore(Protocol):
    def upsert(self, collection: str, ids: list[str], vectors: list[list[float]],
               metas: list[dict]) -> None: ...
    def search(self, collection: str, vector: list[float], top_k: int,
               filters: dict | None = None) -> list[Hit]: ...
    def delete(self, collection: str, doc_id: str) -> None: ...


@runtime_checkable
class LLMProvider(Protocol):
    # 实现可为 async generator（async def + yield），调用方式统一为 async for
    def stream(self, messages: list[dict], **params) -> AsyncIterator[str]: ...
    async def complete(self, messages: list[dict], **params) -> str: ...


@runtime_checkable
class Chunker(Protocol):
    def chunk(self, markdown: str, doc_name: str) -> list[ChunkData]: ...


@runtime_checkable
class Reranker(Protocol):
    def rerank(self, query: str, texts: list[str]) -> list[float]: ...
