"""检索器：叶子命中 -> 父块上下文组装（small-to-big）。

Chroma 中只索引叶子块向量；命中叶子后回 SQLite 取其父块全文作为上下文，
同一父块下的多个叶子命中去重，只返回一次。
"""
from dataclasses import dataclass

from kbase.models import Chunk, Document
from kbase.plugins.base import Embedder, VectorStore


@dataclass
class ContextBlock:
    doc_id: str
    doc_name: str
    heading_path: str
    text: str          # 父块全文（small-to-big 的"big"）
    snippet: str       # 命中的叶子块原文（引用展示用）
    score: float


class Retriever:
    def __init__(self, session_factory, embedder: Embedder, store: VectorStore):
        self._sf = session_factory
        self._embedder = embedder
        self._store = store

    def retrieve(self, kb_id: str, query: str, top_k: int = 5) -> list[ContextBlock]:
        vec = self._embedder.embed([query])[0]
        hits = self._store.search(kb_id, vec, top_k=top_k)
        blocks: list[ContextBlock] = []
        seen_parents: set[str] = set()
        with self._sf() as s:
            for hit in hits:
                leaf = s.get(Chunk, hit.chunk_id)
                if leaf is None:
                    continue
                parent = s.get(Chunk, leaf.parent_id) if leaf.parent_id else leaf
                if parent.id in seen_parents:
                    continue
                seen_parents.add(parent.id)
                doc = s.get(Document, leaf.doc_id)
                blocks.append(ContextBlock(
                    doc_id=leaf.doc_id,
                    doc_name=doc.filename if doc else "未知文档",
                    heading_path=parent.heading_path,
                    text=parent.text,
                    snippet=leaf.text,
                    score=hit.score,
                ))
        return blocks
