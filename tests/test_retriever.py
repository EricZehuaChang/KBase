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


def test_assemble_truncates_oversized_parent_windowed_on_leaf(tmp_path, fake_embedder):
    """D6：父块全文超过 max_parent_chars 时，_assemble 以命中叶子文本在父块中
    首次出现的位置为中心截窗，而不是简单头部截断——否则命中叶子在尾部时窗口
    会截不到叶子内容，答案就丢了关键上下文。截断处加 … 标记。"""
    from kbase.rag.retriever import Retriever

    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()

    leaf_text = "关键叶子命中内容——司局级住宿费标准为每晚六百五十元整。"
    padding_before = "填充文字。" * 1200      # 约 6000 字符，把叶子推到父块尾部
    parent_text = padding_before + leaf_text
    assert len(parent_text) > 6000          # 确认父块确实超过测试用例名称里的 6000 字

    from kbase.models import Chunk
    with factory() as s:
        s.add(Chunk(id="parent1", doc_id="doc1", kb_id="kb1", parent_id=None,
                    heading_path="补贴办法 > 第一章", text=parent_text, is_leaf=False))
        s.add(Chunk(id="leaf1", doc_id="doc1", kb_id="kb1", parent_id="parent1",
                    heading_path="补贴办法 > 第一章", text=leaf_text, is_leaf=True))
        from kbase.models import Document
        s.add(Document(id="doc1", kb_id="kb1", filename="补贴办法.md",
                       content_hash="h1", status="ready"))
        s.commit()

    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    r = Retriever(factory, fake_embedder, store, max_parent_chars=4000)
    blocks = r._assemble([("leaf1", 1.0)], top_k=1)
    assert len(blocks) == 1
    text = blocks[0].text
    assert len(text) <= 4000 + 20            # 上限 + 少量 … 标记余量
    assert leaf_text in text
    assert text.startswith("…")
