from kbase.db import make_session_factory
from kbase.index.keyword import KeywordIndex
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import KnowledgeBase
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import ChromaStore
from kbase.rag.retriever import Retriever, rrf_fuse

MD = """# 差旅办法
## 第一章 交通
乘坐火车出行按新兵办发〔2014〕76号文件执行。
## 第二章 住宿
住宿标准按级别确定。
"""


def test_rrf_fuse_math():
    a = [("x", 0.9), ("y", 0.8)]          # (chunk_id, score) 有序列表
    b = [("y", 5.0), ("z", 4.0)]
    fused = rrf_fuse([a, b], k=60)
    scores = dict(fused)
    # y 双路命中：1/(60+2) + 1/(60+1)
    assert abs(scores["y"] - (1 / 62 + 1 / 61)) < 1e-9
    assert fused[0][0] == "y"             # 双路命中排最前
    assert set(scores) == {"x", "y", "z"}


def _setup(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    kw = KeywordIndex(factory)
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=30, chunk_overlap=0),
                              fake_embedder, store, tmp_path / "files",
                              keyword_index=kw)
    f = tmp_path / "差旅办法.md"
    f.write_text(MD, encoding="utf-8")
    pipeline.ingest_file("kb1", f, "差旅办法.md")
    return factory, fake_embedder, store, kw


def test_hybrid_recalls_keyword_only_hit(tmp_path, fake_embedder):
    """FakeEmbedder 对不同文本给随机向量——文件号查询在稠密路必然命中差，
    但关键词路能精确命中；混合检索必须把它捞回来。"""
    factory, emb, store, kw = _setup(tmp_path, fake_embedder)
    r = Retriever(factory, emb, store, keyword_index=kw)
    blocks = r.retrieve("kb1", "新兵办发〔2014〕76号", top_k=3)
    assert any("76号" in b.text for b in blocks)


def test_pure_dense_mode_unchanged(tmp_path, fake_embedder):
    factory, emb, store, kw = _setup(tmp_path, fake_embedder)
    r = Retriever(factory, emb, store)         # 不传 keyword_index = 纯向量档
    q = "差旅办法.md > 差旅办法 > 第二章 住宿\n住宿标准按级别确定。"
    blocks = r.retrieve("kb1", q, top_k=3)
    assert blocks and "住宿标准" in blocks[0].text


def test_debug_trace(tmp_path, fake_embedder):
    factory, emb, store, kw = _setup(tmp_path, fake_embedder)
    r = Retriever(factory, emb, store, keyword_index=kw)
    result = r.retrieve("kb1", "火车 出行", top_k=3, debug=True)
    assert result.trace is not None
    assert set(result.trace) >= {"dense", "keyword", "fused"}
    assert result.blocks is not None


def test_topk_counts_parent_blocks_not_leaves(tmp_path, fake_embedder):
    """top_k 应是父块数：单文档多叶子霸榜时，去重后仍应补足其他来源。"""
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    kw = KeywordIndex(factory)
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=20, chunk_overlap=0),
                              fake_embedder, store, tmp_path / "files", keyword_index=kw)
    # 文档A：同一章节多个短句叶子（同一父块）；文档B：目标内容
    doc_a = "# 甲文\n## 一章\n差旅标准一。差旅标准二。差旅标准三。差旅标准四。\n"
    doc_b = "# 乙文\n## 一章\n差旅住宿标准五百元。\n"
    (tmp_path / "a.md").write_text(doc_a, encoding="utf-8")
    (tmp_path / "b.md").write_text(doc_b, encoding="utf-8")
    pipeline.ingest_file("kb1", tmp_path / "a.md", "a.md")
    pipeline.ingest_file("kb1", tmp_path / "b.md", "b.md")

    class BiasedReranker:
        def rerank(self, query, texts):
            # 甲文叶子全部给高分，乙文给低分——模拟单文档霸榜
            return [0.9 if "住宿" not in t else 0.5 for t in texts]

    r = Retriever(factory, fake_embedder, store, keyword_index=kw,
                  reranker=BiasedReranker())
    blocks = r.retrieve("kb1", "差旅", top_k=2)
    assert len(blocks) == 2
    assert {b.doc_name for b in blocks} == {"a.md", "b.md"}


class FailingReranker:
    def rerank(self, query, texts):
        raise RuntimeError("TEI reranker 服务不可达（模拟查询期间瞬时掉线）")


def test_rerank_failure_degrades_to_fused_order(tmp_path, fake_embedder):
    """H1 review：重排调用中途失败（TEI 服务瞬时不可达）不应让整次查询变成
    未处理异常/500——应静默降级为融合排序（reranker=None 时的既有路径），
    正常返回 blocks。debug trace 里不应出现 "reranked"（说明确实没走重排
    分支），但仍应有 dense/keyword/fused（降级前的双路召回与融合结果）。"""
    factory, emb, store, kw = _setup(tmp_path, fake_embedder)
    r = Retriever(factory, emb, store, keyword_index=kw, reranker=FailingReranker())

    blocks = r.retrieve("kb1", "新兵办发〔2014〕76号", top_k=3)
    assert blocks   # 没有抛异常，正常拿到融合排序结果

    result = r.retrieve("kb1", "新兵办发〔2014〕76号", top_k=3, debug=True)
    assert "reranked" not in result.trace
    assert set(result.trace) >= {"dense", "keyword", "fused"}
    assert result.blocks
