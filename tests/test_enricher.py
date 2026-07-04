import json

from kbase.db import make_session_factory
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import Chunk, KnowledgeBase
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.enrichers.contextual import ContextualEnricher
from kbase.plugins.vectorstores.chroma_store import ChromaStore

MD = "# 补贴办法\n## 第一章\n满两年可申领。\n"


class FakeLLM:
    async def complete(self, messages, **params):
        return "该片段属于补贴办法申领条件部分"


def test_enricher_fills_context():
    from kbase.plugins.base import ChunkData
    leaves = [ChunkData(id="c1", text="满两年可申领。", heading_path="h", parent_id="p")]
    enricher = ContextualEnricher(llm=FakeLLM())
    out = enricher.enrich("补贴办法.md", MD, leaves)
    assert out[0].meta["enrich_context"] == "该片段属于补贴办法申领条件部分"


def test_pipeline_enrich_persists_and_embeds(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库",
                            config=json.dumps({"enrich": {"enabled": True}})))
        s.commit()
    calls = []

    class SpyEmbedder:
        dimension = 8
        def embed(self, texts):
            calls.extend(texts)
            return fake_embedder.embed(texts)

    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=200, chunk_overlap=0),
                              SpyEmbedder(), ChromaStore(persist_dir=str(tmp_path / "c")),
                              tmp_path / "f", enricher=ContextualEnricher(llm=FakeLLM()))
    f = tmp_path / "a.md"
    f.write_text(MD, encoding="utf-8")
    doc_id = pipeline.ingest_file("kb1", f, "a.md")
    with factory() as s:
        leaf = s.query(Chunk).filter_by(doc_id=doc_id, is_leaf=True).first()
        assert leaf.enrich_context == "该片段属于补贴办法申领条件部分"
    assert any(t.startswith("该片段属于") for t in calls)   # 增强文本参与向量化


def test_enrich_failure_falls_back(tmp_path, fake_embedder):
    class BrokenLLM:
        async def complete(self, messages, **params):
            raise RuntimeError("api down")

    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库",
                            config=json.dumps({"enrich": {"enabled": True}})))
        s.commit()
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=200, chunk_overlap=0),
                              fake_embedder, ChromaStore(persist_dir=str(tmp_path / "c")),
                              tmp_path / "f", enricher=ContextualEnricher(llm=BrokenLLM()))
    f = tmp_path / "a.md"
    f.write_text(MD, encoding="utf-8")
    doc_id = pipeline.ingest_file("kb1", f, "a.md")
    with factory() as s:
        from kbase.models import Document
        assert s.get(Document, doc_id).status == "ready"    # 失败回退不阻塞
