from sqlalchemy import inspect, text

from kbase.db import make_session_factory


def test_migrations_add_columns_and_tables(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        insp = inspect(s.get_bind())
        chunk_cols = {c["name"] for c in insp.get_columns("chunks")}
        assert "enrich_context" in chunk_cols
        kb_cols = {c["name"] for c in insp.get_columns("knowledge_bases")}
        assert "config" in kb_cols
        doc_cols = {c["name"] for c in insp.get_columns("documents")}
        assert "ocr_confidence" in doc_cols
        tables = set(insp.get_table_names())
        assert {"conversations", "messages", "providers", "app_settings"} <= tables
        assert s.execute(text(
            "SELECT name FROM sqlite_master WHERE name='chunks_fts'"
        )).scalar() == "chunks_fts"


def test_migrations_idempotent(tmp_path):
    url = f"sqlite:///{tmp_path}/kb.sqlite"
    make_session_factory(url)
    factory = make_session_factory(url)     # 第二次不应报错
    with factory() as s:
        s.execute(text(
            "INSERT INTO chunks_fts(chunk_id, kb_id, doc_id, text) "
            "VALUES ('c1','kb1','d1','测试 内容')"))
        s.commit()
        got = s.execute(text(
            "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH '测试'"
        )).scalar()
        assert got == "c1"


def test_existing_m1_db_upgrades(tmp_path):
    """模拟 M1 旧库（无新列）→ 迁移后可用。"""
    import sqlite3
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE knowledge_bases (id VARCHAR(36) PRIMARY KEY, name VARCHAR(200), created_at DATETIME);
        CREATE TABLE documents (id VARCHAR(36) PRIMARY KEY, kb_id VARCHAR(36), filename VARCHAR(500),
            content_hash VARCHAR(64), status VARCHAR(20), error TEXT, created_at DATETIME);
        CREATE TABLE chunks (id VARCHAR(36) PRIMARY KEY, doc_id VARCHAR(36), kb_id VARCHAR(36),
            parent_id VARCHAR(36), prev_id VARCHAR(36), next_id VARCHAR(36),
            heading_path TEXT, text TEXT, is_leaf BOOLEAN);
    """)
    conn.close()
    factory = make_session_factory(f"sqlite:///{db}")
    with factory() as s:
        insp = inspect(s.get_bind())
        assert "enrich_context" in {c["name"] for c in insp.get_columns("chunks")}
