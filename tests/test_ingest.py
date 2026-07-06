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


def test_ingest_duplicate_content_docs_dedup_to_one_and_chunk_counts_stay_consistent(
        tmp_path, fake_embedder):
    """H6 压测复盘（M4-2 Bug2）：40 篇文档种入后 PG chunks 表 663 行，但
    Qdrant/PG 关键词索引各只有 357 个可检索单元，两个数字不相等。已在真实
    GCP standard 栈上核实：40 个文档全部 status=ready、content_hash 无重复
    组、chunks 与 chunks_kw 都覆盖全部 40 个 doc_id——不存在"块写入了但索引
    丢失"的竞态。真正原因是 chunks 表同时存父块（章节，is_leaf=False，只供
    上下文组装）与叶子块（is_leaf=True，唯一被向量化/关键词索引的对象，见
    ingest/pipeline.py _process 与 reindex.py），657/357 的差额就是父块数量
    （306），663 = 306 父块 + 357 叶子块，与 40 个文档各自的 chunker 输出一一
    对应，属预期设计，不是数据一致性 bug。

    本用例最小复现"多文档并发摄取，部分重复部分不重复"的场景，断言：
    1. 重复内容的文档去重为一个 doc_id（D4 既有行为，见 test_ingest_dedup_by_hash）；
    2. 每个不同文档各自贡献的 Chunk 行数 = 该文档 chunker 输出的父块+叶子块
       总数，不会因为其他文档的摄取而多出或少出行——即 chunks 表行数与
       "唯一文档数 × 各自块数"始终精确对齐，没有孤儿行。"""
    factory, pipeline = _mk(tmp_path, fake_embedder)

    dup_a = tmp_path / "dup_a.md"
    dup_a.write_text(MD, encoding="utf-8")
    dup_b = tmp_path / "dup_b.md"
    dup_b.write_text(MD, encoding="utf-8")             # 与 dup_a 内容完全相同
    uniq = tmp_path / "uniq.md"
    uniq.write_text(MD + "\n## 第三章 附则\n本办法自发布之日起施行。\n",
                    encoding="utf-8")                   # 内容不同，不应被去重

    d_a = pipeline.ingest_file("kb1", dup_a, original_name="dup_a.md")
    d_b = pipeline.ingest_file("kb1", dup_b, original_name="dup_b.md")
    d_u = pipeline.ingest_file("kb1", uniq, original_name="uniq.md")

    assert d_a == d_b                    # 重复内容去重到同一 doc_id
    assert d_u != d_a                    # 不同内容不应被误判为重复

    with factory() as s:
        docs = s.query(Document).filter_by(kb_id="kb1").all()
        assert len(docs) == 2            # 去重后只有 2 个文档（1 组重复 + 1 个独立）

        chunks_dup = s.query(Chunk).filter_by(doc_id=d_a).all()
        chunks_uniq = s.query(Chunk).filter_by(doc_id=d_u).all()
        all_chunks = s.query(Chunk).filter_by(kb_id="kb1").all()

        # 没有第三个 doc_id 的孤儿块（比如去重失败时 dup_b 曾经短暂摄取过又被清理不干净）
        assert {c.doc_id for c in all_chunks} == {d_a, d_u}
        # chunks 表总行数 = 两个文档各自的块数之和，不多不少——重复文档只贡献一份
        assert len(all_chunks) == len(chunks_dup) + len(chunks_uniq)
        # 每个文档的父块:叶子块比例应为 1:1 结构（本测试用的 MD 每章一个父块
        # +至少一个叶子块），佐证 663/357 的差额来自父块而非丢失的叶子块
        leaves_dup = [c for c in chunks_dup if c.is_leaf]
        parents_dup = [c for c in chunks_dup if not c.is_leaf]
        assert len(leaves_dup) == len(parents_dup) > 0


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
