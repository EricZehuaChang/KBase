import pytest
from sqlalchemy.exc import IntegrityError

from kbase.db import make_session_factory
from kbase.models import ApiKey, AuditLog, Chunk, Document, KnowledgeBase, User


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


def test_user_api_key_audit_log_roundtrip(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        user = User(id="u1", username="admin", password_hash="hashed",
                    role="admin", disabled=False)
        key = ApiKey(id="k1", name="mcp-key", prefix="kbase_ak", key_hash="deadbeef",
                     role="editor", revoked=False)
        log = AuditLog(id="a1", actor="admin", action="POST /api/kb",
                       resource="kb1", detail='{"name":"x"}', ip="127.0.0.1")
        s.add_all([user, key, log])
        s.commit()
    with factory() as s:
        got_user = s.get(User, "u1")
        assert got_user.username == "admin" and got_user.role == "admin"
        assert not got_user.disabled
        got_key = s.get(ApiKey, "k1")
        assert got_key.prefix == "kbase_ak" and not got_key.revoked
        got_log = s.get(AuditLog, "a1")
        assert got_log.action == "POST /api/kb" and got_log.ip == "127.0.0.1"


def test_username_unique_constraint(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(User(id="u1", username="admin", password_hash="h1", role="admin"))
        s.commit()
    with factory() as s:
        s.add(User(id="u2", username="admin", password_hash="h2", role="viewer"))
        with pytest.raises(IntegrityError):
            s.commit()
