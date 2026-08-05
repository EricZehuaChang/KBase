"""方案卡元数据过滤全链路（ztenith 流水线 P0-2/P0-3）：
① 摄取：front matter → 向量库 payload + Chunk.meta（任一 chunk 可回查元数据）；
② 检索：canonical filters（字段间 AND、列表内 OR）——稠密路向量库原生过滤
   （Chroma 超采后过滤 / Qdrant MatchAny，契约测试双档对齐）、关键词路
   Chunk.meta 后过滤（chunk_meta_matches 纯函数）；
③ API：/search 带 filters 返回集合正确变化。"""
import json

import pytest

from kbase.db import make_session_factory
from kbase.index.keyword import KeywordIndex
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import Chunk, KnowledgeBase
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import ChromaStore
from kbase.plugins.vectorstores.qdrant_store import QdrantStore
from kbase.rag.retriever import chunk_meta_matches

CARD_RETAIL = """---
id: card-retail
industry: 零售
data_entities: [订单, 库存]
---
# 零售订单方案

订单列表页与库存同步的做法。
"""

CARD_FINANCE = """---
id: card-finance
industry: 金融
data_entities: [账户, 流水]
---
# 金融对账方案

账户流水核对的做法。
"""


def _mk_pipeline(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="卡库"))
        s.commit()
    pipeline = IngestPipeline(
        session_factory=factory,
        chunker=StructureChunker(chunk_size=200, chunk_overlap=0),
        embedder=fake_embedder,
        store=ChromaStore(persist_dir=str(tmp_path / "chroma")),
        files_dir=tmp_path / "files",
    )
    return factory, pipeline


def test_ingest_front_matter_lands_in_chunk_meta_and_body_indexed(
        tmp_path, fake_embedder):
    factory, pipeline = _mk_pipeline(tmp_path, fake_embedder)
    f = tmp_path / "card-retail.md"
    f.write_text(CARD_RETAIL, encoding="utf-8")
    doc_id = pipeline.ingest_file("kb1", f, original_name="card-retail.md")

    with factory() as s:
        chunks = s.query(Chunk).filter_by(doc_id=doc_id).all()
        assert chunks, "正文照常分块"
        for c in chunks:
            meta = json.loads(c.meta)
            assert meta["industry"] == "零售"
            assert meta["data_entities"] == ["订单", "库存"]
        # front matter 不进正文（不被分块向量化）
        assert not any("card-retail" in c.text for c in chunks)
    # 落盘的 content.md 是剥离 front matter 后的正文
    content = (tmp_path / "files" / doc_id / "content.md").read_text(encoding="utf-8")
    assert content.startswith("# 零售订单方案")


@pytest.fixture(params=["chroma", "qdrant-memory"])
def store(request, tmp_path):
    if request.param == "chroma":
        return ChromaStore(persist_dir=str(tmp_path / "chroma-flt"))
    return QdrantStore(location=":memory:")


def test_vectorstore_canonical_filters(store, fake_embedder):
    """双档契约：标量等值、列表字段"包含任一"、字段间 AND。"""
    vecs = fake_embedder.embed(["甲", "乙", "丙"])
    store.upsert("kb1", ids=["c1", "c2", "c3"], vectors=vecs, metas=[
        {"doc_id": "d1", "industry": "零售", "data_entities": ["订单", "库存"]},
        {"doc_id": "d1", "industry": "金融", "data_entities": ["账户"]},
        {"doc_id": "d2", "industry": "零售", "data_entities": ["会员"]},
    ])
    # 标量等值
    hits = store.search("kb1", vecs[0], top_k=3, filters={"industry": "零售"})
    assert {h.chunk_id for h in hits} == {"c1", "c3"}
    # 列表值 = OR
    hits = store.search("kb1", vecs[0], top_k=3,
                        filters={"industry": ["零售", "金融"]})
    assert {h.chunk_id for h in hits} == {"c1", "c2", "c3"}
    # 多值字段"包含任一"
    hits = store.search("kb1", vecs[0], top_k=3,
                        filters={"data_entities": "订单"})
    assert {h.chunk_id for h in hits} == {"c1"}
    # 字段间 AND
    hits = store.search("kb1", vecs[0], top_k=3,
                        filters={"industry": "零售", "data_entities": "会员"})
    assert {h.chunk_id for h in hits} == {"c3"}
    # 无命中
    assert store.search("kb1", vecs[0], top_k=3,
                        filters={"industry": "制造"}) == []


def test_chunk_meta_matches_semantics():
    meta = json.dumps({"industry": "零售", "data_entities": ["订单", "库存"],
                       "client_size": 500}, ensure_ascii=False)
    assert chunk_meta_matches(meta, {"industry": "零售"})
    assert chunk_meta_matches(meta, {"industry": ["金融", "零售"]})
    assert chunk_meta_matches(meta, {"data_entities": "库存"})
    assert chunk_meta_matches(meta, {"client_size": "500"})     # str/int 互认
    assert not chunk_meta_matches(meta, {"industry": "金融"})
    assert not chunk_meta_matches(meta, {"missing": "x"})       # 缺字段=不匹配
    assert not chunk_meta_matches(None, {"industry": "零售"})   # 非方案卡文档
    assert not chunk_meta_matches("not-json", {"industry": "零售"})


def test_end_to_end_search_with_filters(tmp_path, fake_embedder):
    """摄取两张不同行业的卡后，带/不带行业过滤返回集合正确变化
    （含关键词路后过滤——pipeline 配 KeywordIndex 时两路都过滤）。"""
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb2.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="卡库"))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / "chroma-e2e"))
    kw = KeywordIndex(factory)
    pipeline = IngestPipeline(
        session_factory=factory,
        chunker=StructureChunker(chunk_size=200, chunk_overlap=0),
        embedder=fake_embedder, store=store,
        files_dir=tmp_path / "files", keyword_index=kw,
    )
    for name, text in [("retail.md", CARD_RETAIL), ("finance.md", CARD_FINANCE)]:
        f = tmp_path / name
        f.write_text(text, encoding="utf-8")
        pipeline.ingest_file("kb1", f, original_name=name)

    from kbase.rag.retriever import Retriever
    retriever = Retriever(session_factory=factory, embedder=fake_embedder,
                          store=store, keyword_index=kw)
    all_blocks = retriever.retrieve("kb1", "方案", top_k=5)
    assert {b.doc_name for b in all_blocks} == {"retail.md", "finance.md"}
    retail_only = retriever.retrieve("kb1", "方案", top_k=5,
                                     filters={"industry": "零售"})
    assert retail_only and {b.doc_name for b in retail_only} == {"retail.md"}
    none = retriever.retrieve("kb1", "方案", top_k=5,
                              filters={"industry": "制造"})
    assert none == []
