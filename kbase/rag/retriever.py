"""检索器：分级管道 —— 双路召回（稠密+关键词）-> RRF 融合 -> (重排，A4 接线) -> 父块组装。

Chroma 中只索引叶子块向量；命中叶子后回 SQLite 取其父块全文作为上下文，
同一父块下的多个叶子命中去重，只返回一次（small-to-big）。

关键词路与稠密路的分数不可比（bm25 变体 vs 余弦），融合一律走 RRF 按名次，
不直接加权分数。关键词路独有的候选没有稠密分数，为了让下游（阈值/展示）
拿到统一语义的余弦，用 ChromaStore 的存量向量补算。
"""
import math
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


@dataclass
class RetrievalResult:
    blocks: list        # list[ContextBlock]
    trace: dict | None = None


def rrf_fuse(ranked_lists: list[list[tuple[str, float]]], k: int = 60
             ) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion。输入各路 (chunk_id, score) 有序列表（已按相关性降序），
    输出按融合分降序的 (chunk_id, fused_score)。分数值本身不参与计算，只用名次——
    这是 RRF 的核心思路，用来把量纲不同、不可比较的多路排名合并为统一序。"""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (cid, _s) in enumerate(ranked, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class Retriever:
    def __init__(self, session_factory, embedder: Embedder, store: VectorStore,
                 keyword_index=None, reranker=None,
                 candidates: int = 20, rrf_k: int = 60):
        self._sf = session_factory
        self._embedder = embedder
        self._store = store
        self._kw = keyword_index
        self._reranker = reranker
        self._candidates = candidates
        self._rrf_k = rrf_k

    @property
    def rerank_active(self) -> bool:
        return self._reranker is not None

    def retrieve(self, kb_id: str, query: str, top_k: int = 5, debug: bool = False):
        """debug=False 返回 list[ContextBlock]（向后兼容）；
        debug=True 返回 RetrievalResult(blocks, trace)。"""
        trace: dict = {}
        vec = self._embedder.embed([query])[0]
        dense_hits = self._store.search(kb_id, vec, top_k=self._candidates)
        dense = [(h.chunk_id, h.score) for h in dense_hits]
        cosine = {h.chunk_id: h.score for h in dense_hits}
        trace["dense"] = dense

        if self._kw is not None:
            kw_hits = self._kw.search(kb_id, query, top_k=self._candidates)
            keyword = [(h.chunk_id, h.score) for h in kw_hits]
            trace["keyword"] = keyword
            fused = rrf_fuse([dense, keyword], k=self._rrf_k)[: self._candidates]
        else:
            fused = dense[: self._candidates]
        trace["fused"] = fused

        candidate_ids = [cid for cid, _ in fused]
        # 关键词路独有候选补算余弦（用 Chroma 存量向量，保证阈值语义统一）
        missing = [cid for cid in candidate_ids if cid not in cosine]
        if missing:
            cosine.update(self._cosine_from_store(kb_id, missing, vec))

        if self._reranker is not None:
            texts = self._leaf_texts(candidate_ids)
            scores = self._reranker.rerank(query, [texts.get(cid, "") for cid in candidate_ids])
            reranked = sorted(zip(candidate_ids, scores),
                              key=lambda kv: kv[1], reverse=True)
            trace["reranked"] = reranked
            ordered = [(cid, s) for cid, s in reranked[:top_k]]
        else:
            ordered = [(cid, cosine.get(cid, 0.0)) for cid in candidate_ids[:top_k]]

        blocks = self._assemble(ordered)
        if debug:
            return RetrievalResult(blocks=blocks, trace=trace)
        return blocks

    def _cosine_from_store(self, kb_id: str, ids: list[str], query_vec: list[float]
                            ) -> dict[str, float]:
        vectors = self._store.get_vectors(kb_id, ids)
        return {cid: _cosine(query_vec, v) for cid, v in vectors.items()}

    def _leaf_texts(self, ids: list[str]) -> dict[str, str]:
        """批量取叶子块文本（heading_path+"\n"+text，与向量化时一致），供重排使用。"""
        if not ids:
            return {}
        with self._sf() as s:
            leaves = s.query(Chunk).filter(Chunk.id.in_(ids)).all()
            return {c.id: f"{c.heading_path}\n{c.text}" for c in leaves}

    def _assemble(self, ordered: list[tuple[str, float]]) -> list[ContextBlock]:
        """叶子命中 -> 父块上下文组装（small-to-big，M1 既有逻辑）。
        按 ordered 顺序遍历，同一父块下的多个叶子命中去重，只返回一次；
        score 取 ordered 中该叶子对应的分数（融合/重排/余弦，视管道档位而定）。"""
        blocks: list[ContextBlock] = []
        seen_parents: set[str] = set()
        with self._sf() as s:
            for chunk_id, score in ordered:
                leaf = s.get(Chunk, chunk_id)
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
                    score=score,
                ))
        return blocks
