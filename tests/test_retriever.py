from kbase.db import make_session_factory
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import KnowledgeBase
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import ChromaStore
from kbase.rag.retriever import Retriever

MD = """# 补贴办法
## 第一章 申领条件
连续工作满两年可申领住房补贴。
补贴对象为在编在岗人员。
## 第二章 标准
每月补贴一千元。
"""


def _setup(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=20, chunk_overlap=0),
                              fake_embedder, store, tmp_path / "files")
    f = tmp_path / "补贴办法.md"
    f.write_text(MD, encoding="utf-8")
    pipeline.ingest_file("kb1", f, "补贴办法.md")
    return Retriever(factory, fake_embedder, store)


def test_retrieve_returns_parent_context(tmp_path, fake_embedder):
    r = _setup(tmp_path, fake_embedder)
    # FakeEmbedder 是 hash 确定性的：用与某叶子块向量化文本一致的查询保证命中
    query = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    blocks = r.retrieve("kb1", query, top_k=3)
    assert blocks
    top = blocks[0]
    # small-to-big：返回的是父块全文，包含叶子块之外的兄弟内容
    assert "连续工作满两年" in top.text
    assert "在编在岗" in top.text
    assert top.doc_name == "补贴办法.md"
    assert "第一章" in top.heading_path


def test_parent_dedup(tmp_path, fake_embedder):
    """同一父块下多个叶子命中时，父块只出现一次。"""
    r = _setup(tmp_path, fake_embedder)
    query = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    blocks = r.retrieve("kb1", query, top_k=10)
    paths = [b.heading_path for b in blocks]
    assert len(paths) == len(set(paths))
