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
