from kbase.db import make_session_factory
from kbase.index.keyword import KeywordIndex
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import KnowledgeBase
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import ChromaStore
from kbase.reindex import reindex_kb

MD = "# 办法\n## 一章\n新兵办发〔2014〕76号相关内容。\n"


def test_reindex_backfills_fts(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / "c"))
    # 摄取时不带 keyword_index（模拟 M1 存量库）
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=200, chunk_overlap=0),
                              fake_embedder, store, tmp_path / "f")
    f = tmp_path / "a.md"
    f.write_text(MD, encoding="utf-8")
    pipeline.ingest_file("kb1", f, "a.md")

    kw = KeywordIndex(factory)
    assert kw.search("kb1", "76号", top_k=3) == []          # 存量库无 FTS
    n = reindex_kb(factory, kw, fake_embedder, store, kb_id="kb1")
    assert n > 0
    assert kw.search("kb1", "76号", top_k=3)                 # 回填后命中
