"""启动时幂等迁移：SQLite 轻量 schema 演进，不引入 Alembic。
create_all 只建缺失的表；本模块补既有表的缺列与 FTS5 虚拟表。"""
from sqlalchemy import inspect, text

_COLUMN_MIGRATIONS = [
    ("chunks", "enrich_context", "TEXT"),
    ("knowledge_bases", "config", "TEXT"),
    ("documents", "ocr_confidence", "REAL"),
    ("documents", "source_path", "TEXT"),
    ("messages", "seq", "INTEGER"),
]

_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
    "chunk_id UNINDEXED, kb_id UNINDEXED, doc_id UNINDEXED, text, "
    "tokenize='unicode61')"
)


def run_migrations(engine) -> None:
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl_type in _COLUMN_MIGRATIONS:
            if table in insp.get_table_names():
                cols = {c["name"] for c in insp.get_columns(table)}
                if column not in cols:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        conn.execute(text(_FTS_DDL))
