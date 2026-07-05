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

# D4：documents 表按 (kb_id, content_hash) 去重的唯一索引——防止并发摄取
# 同一文件产生重复行（pipeline.ingest_file 的"先查后插"在两个线程之间存在
# 竞态窗口，唯一约束是最终一致性的兜底）。
_DEDUP_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_doc_kb_hash "
    "ON documents(kb_id, content_hash)"
)

# 建唯一索引前先幂等清理历史重复行（保留 created_at 最早的一条，
# created_at 相同则以更小的 rowid——即更早插入——为准兜底）。
# 否则老库若已有重复 (kb_id, content_hash) 会导致 CREATE UNIQUE INDEX 报错。
_DEDUP_ROWS_SQL = (
    "DELETE FROM documents WHERE rowid NOT IN ("
    "  SELECT rowid FROM ("
    "    SELECT rowid, kb_id, content_hash,"
    "           ROW_NUMBER() OVER ("
    "             PARTITION BY kb_id, content_hash"
    "             ORDER BY created_at ASC, rowid ASC"
    "           ) AS rn"
    "    FROM documents"
    "  ) WHERE rn = 1"
    ")"
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
        if "documents" in insp.get_table_names():
            conn.execute(text(_DEDUP_ROWS_SQL))
            conn.execute(text(_DEDUP_INDEX_DDL))
