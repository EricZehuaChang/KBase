import threading
from pathlib import Path

from kbase.db import make_session_factory
from kbase.index.keyword import KeywordIndex
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import Chunk, Document, KnowledgeBase
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import ChromaStore

MD = """# 补贴办法
## 第一章 申领条件
连续工作满两年可申领住房补贴。
## 第二章 标准
每月补贴一千元。
"""


def _mk(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="测试库"))
        s.commit()
    pipeline = IngestPipeline(
        session_factory=factory,
        chunker=StructureChunker(chunk_size=200, chunk_overlap=0),
        embedder=fake_embedder,
        store=ChromaStore(persist_dir=str(tmp_path / "chroma")),
        files_dir=tmp_path / "files",
    )
    return factory, pipeline


def test_ingest_md_file(tmp_path, fake_embedder):
    factory, pipeline = _mk(tmp_path, fake_embedder)
    f = tmp_path / "补贴办法.md"
    f.write_text(MD, encoding="utf-8")

    doc_id = pipeline.ingest_file("kb1", f, original_name="补贴办法.md")

    with factory() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == "ready"
        chunks = s.query(Chunk).filter_by(doc_id=doc_id).all()
        parents = [c for c in chunks if not c.is_leaf]
        leaves = [c for c in chunks if c.is_leaf]
        assert len(parents) == 2 and len(leaves) >= 2
    # markdown 中间产物已落盘
    assert (tmp_path / "files" / doc_id / "content.md").exists()


def test_ingest_dedup_by_hash(tmp_path, fake_embedder):
    factory, pipeline = _mk(tmp_path, fake_embedder)
    f = tmp_path / "a.md"
    f.write_text(MD, encoding="utf-8")
    d1 = pipeline.ingest_file("kb1", f, original_name="a.md")
    d2 = pipeline.ingest_file("kb1", f, original_name="a-重复.md")
    assert d1 == d2                      # 命中去重，返回已有文档


def test_ingest_failure_isolated(tmp_path, fake_embedder):
    factory, pipeline = _mk(tmp_path, fake_embedder)
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"\x00\x01not a real docx")
    doc_id = pipeline.ingest_file("kb1", bad, original_name="bad.docx")
    with factory() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == "failed"
        assert doc.error                 # 有失败原因，且没有抛异常


def test_ingest_indexes_leaves_into_keyword_index(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="测试库"))
        s.commit()
    kw = KeywordIndex(factory)
    pipeline = IngestPipeline(
        session_factory=factory,
        chunker=StructureChunker(chunk_size=200, chunk_overlap=0),
        embedder=fake_embedder,
        store=ChromaStore(persist_dir=str(tmp_path / "chroma")),
        files_dir=tmp_path / "files",
        keyword_index=kw,
    )
    f = tmp_path / "补贴办法.md"
    f.write_text(MD, encoding="utf-8")
    doc_id = pipeline.ingest_file("kb1", f, original_name="补贴办法.md")

    with factory() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == "ready"

    hits = kw.search("kb1", "住房补贴的申领条件", top_k=3)
    assert hits


def test_ingest_concurrent_same_file_dedup_single_row(tmp_path, fake_embedder):
    """D4：两个线程同时摄取同一份文件（同内容→同 content_hash），
    unique index (kb_id, content_hash) 保证最终 documents 表只有一行——
    第二个线程的 INSERT 撞唯一约束触发 IntegrityError，pipeline 捕获后
    重查返回已有文档 id，而不是让异常冒出或产生重复行。"""
    factory, pipeline = _mk(tmp_path, fake_embedder)
    f = tmp_path / "并发.md"
    f.write_text(MD, encoding="utf-8")

    results = [None, None]

    def worker(i):
        results[i] = pipeline.ingest_file("kb1", f, original_name=f"并发-{i}.md")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results[0] is not None and results[1] is not None
    assert results[0] == results[1]     # 两线程拿到同一个既有文档 id
    with factory() as s:
        rows = s.query(Document).filter_by(kb_id="kb1").all()
        assert len(rows) == 1
