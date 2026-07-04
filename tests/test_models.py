from kbase.db import make_session_factory
from kbase.models import Chunk, Document, KnowledgeBase


def test_roundtrip(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        kb = KnowledgeBase(id="kb1", name="政策库")
        doc = Document(id="d1", kb_id="kb1", filename="a.docx",
                       content_hash="abc", status="ready")
        parent = Chunk(id="p1", doc_id="d1", kb_id="kb1", heading_path="a > 一章",
                       text="全文", is_leaf=False)
        leaf = Chunk(id="c1", doc_id="d1", kb_id="kb1", parent_id="p1",
                     heading_path="a > 一章", text="片段", is_leaf=True)
        s.add_all([kb, doc, parent, leaf])
        s.commit()
    with factory() as s:
        got = s.get(Chunk, "c1")
        assert got.parent_id == "p1"
        assert s.get(Document, "d1").status == "ready"


def test_duplicate_hash_lookup(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="x"))
        s.add(Document(id="d1", kb_id="kb1", filename="a.docx",
                       content_hash="same", status="ready"))
        s.commit()
        dup = s.query(Document).filter_by(kb_id="kb1", content_hash="same").first()
        assert dup is not None
