from kbase.plugins.vectorstores.chroma_store import ChromaStore


def _mk(tmp_path, fake_embedder):
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    vecs = fake_embedder.embed(["甲", "乙", "丙"])
    store.upsert("kb1",
                 ids=["c1", "c2", "c3"],
                 vectors=vecs,
                 metas=[{"doc_id": "d1"}, {"doc_id": "d1"}, {"doc_id": "d2"}])
    return store, vecs


def test_search_returns_hits(tmp_path, fake_embedder):
    store, vecs = _mk(tmp_path, fake_embedder)
    hits = store.search("kb1", vecs[0], top_k=2)
    assert hits[0].chunk_id == "c1"          # 自身向量最相近
    assert hits[0].score >= hits[1].score


def test_filter_by_doc(tmp_path, fake_embedder):
    store, vecs = _mk(tmp_path, fake_embedder)
    hits = store.search("kb1", vecs[0], top_k=3, filters={"doc_id": "d2"})
    assert {h.chunk_id for h in hits} == {"c3"}


def test_upsert_empty_batch_is_noop(tmp_path):
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    store.upsert("kb1", ids=[], vectors=[], metas=[])   # 不应抛异常
    assert store.search("kb1", [0.0] * 8, top_k=3) == []


def test_delete_by_doc(tmp_path, fake_embedder):
    store, vecs = _mk(tmp_path, fake_embedder)
    store.delete("kb1", doc_id="d1")
    hits = store.search("kb1", vecs[0], top_k=3)
    assert {h.chunk_id for h in hits} == {"c3"}
