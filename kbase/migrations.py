"""启动时幂等迁移：轻量 schema 演进，不引入 Alembic。
create_all 只建缺失的表；本模块补既有表的缺列、关键词索引后端专属结构。

方言感知（M4-2 H3）：run_migrations 按 engine.dialect.name 分派——
- sqlite：既有路径不变（FTS5 虚拟表 + rowid 去重）。
- postgresql：跳过 FTS5（PG 侧关键词索引改用 chunks_kw 表 + tsvector 列
  + GIN 索引，见 kbase/index/keyword_pg.py），去重查询没有 rowid 可用，
  改用 ctid 做同 created_at 时的并列 tiebreaker。
列缺失守卫两种方言共用同一段逻辑：SQLAlchemy inspector 的
get_table_names/get_columns 底层对 PG 走 information_schema.columns 反射，
无需为 PG 单独写列探测 SQL。
"""
from sqlalchemy import inspect, text

_COLUMN_MIGRATIONS = [
    ("chunks", "enrich_context", "TEXT"),
    ("knowledge_bases", "config", "TEXT"),
    ("documents", "ocr_confidence", "REAL"),
    ("documents", "source_path", "TEXT"),
    ("messages", "seq", "INTEGER"),
    # M5-1 F2：会话归属列，鉴权改造前建的库没有这一列，老库存量会话补列后
    # 天然是 NULL（历史遗留、两边可见，见 kbase/conversations.py 归属过滤）。
    ("conversations", "user_id", "TEXT"),
    # M5-2：provider 页面直配密钥列。老库补列后为 NULL=沿用 api_key_env，
    # 行为与升级前完全一致。
    ("providers", "api_key", "TEXT"),
    # M5-2：块级源文件页码（引用定位）。老库存量块补列后为 NULL=不支持定位，
    # 重新摄取（或重试）后回填。
    ("chunks", "page", "INTEGER"),
]

_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
    "chunk_id UNINDEXED, kb_id UNINDEXED, doc_id UNINDEXED, text, "
    "tokenize='unicode61')"
)

# D4：documents 表按 (kb_id, content_hash) 去重的唯一索引——防止并发摄取
# 同一文件产生重复行（pipeline.ingest_file 的"先查后插"在两个线程之间存在
# 竞态窗口，唯一约束是最终一致性的兜底）。
# CREATE UNIQUE INDEX IF NOT EXISTS 语法两种方言通用（PG 9.5+ 支持）。
_DEDUP_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_doc_kb_hash "
    "ON documents(kb_id, content_hash)"
)

# 建唯一索引前先幂等清理历史重复行（保留 created_at 最早的一条，
# created_at 相同则以更小的 rowid——即更早插入——为准兜底）。
# 否则老库若已有重复 (kb_id, content_hash) 会导致 CREATE UNIQUE INDEX 报错。
# SQLite 专属：rowid 是 SQLite 内置的隐藏行标识。
_DEDUP_ROWS_SQL_SQLITE = (
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

# PostgreSQL 专属：没有 rowid，用 ctid（页内物理位置，本迁移在单条语句内
# 读写同一张表，足够作为并列 tiebreaker；不依赖 ctid 跨语句/VACUUM 稳定）。
_DEDUP_ROWS_SQL_POSTGRESQL = (
    "DELETE FROM documents WHERE ctid NOT IN ("
    "  SELECT ctid FROM ("
    "    SELECT ctid, kb_id, content_hash,"
    "           ROW_NUMBER() OVER ("
    "             PARTITION BY kb_id, content_hash"
    "             ORDER BY created_at ASC, id ASC"
    "           ) AS rn"
    "    FROM documents"
    "  ) t WHERE rn = 1"
    ")"
)

# PG 关键词索引（kbase/index/keyword_pg.py）的后备表：chunk_id 主键，
# tsv 列存分词后 to_tsvector 结果，GIN 索引加速 @@ 匹配。
_CHUNKS_KW_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS chunks_kw ("
    "chunk_id VARCHAR(36) PRIMARY KEY, "
    "kb_id VARCHAR(36) NOT NULL, "
    "doc_id VARCHAR(36) NOT NULL, "
    "tsv TSVECTOR)"
)
_CHUNKS_KW_GIN_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_chunks_kw_tsv ON chunks_kw USING GIN(tsv)"
)
_CHUNKS_KW_KB_IDX_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_chunks_kw_kb_id ON chunks_kw(kb_id)"
)


def _run_column_guards(conn, insp) -> None:
    tables = insp.get_table_names()
    for table, column, ddl_type in _COLUMN_MIGRATIONS:
        if table in tables:
            cols = {c["name"] for c in insp.get_columns(table)}
            if column not in cols:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def _run_sqlite_migrations(conn, insp) -> None:
    _run_column_guards(conn, insp)
    conn.execute(text(_FTS_DDL))
    if "documents" in insp.get_table_names():
        conn.execute(text(_DEDUP_ROWS_SQL_SQLITE))
        conn.execute(text(_DEDUP_INDEX_DDL))


def _run_postgresql_migrations(conn, insp) -> None:
    _run_column_guards(conn, insp)
    conn.execute(text(_CHUNKS_KW_TABLE_DDL))
    conn.execute(text(_CHUNKS_KW_GIN_DDL))
    conn.execute(text(_CHUNKS_KW_KB_IDX_DDL))
    if "documents" in insp.get_table_names():
        conn.execute(text(_DEDUP_ROWS_SQL_POSTGRESQL))
        conn.execute(text(_DEDUP_INDEX_DDL))


_DIALECT_MIGRATIONS = {
    "sqlite": _run_sqlite_migrations,
    "postgresql": _run_postgresql_migrations,
}


def run_migrations(engine) -> None:
    insp = inspect(engine)
    dialect = engine.dialect.name
    try:
        migrate = _DIALECT_MIGRATIONS[dialect]
    except KeyError:
        raise ValueError(
            f"run_migrations: 不支持的方言 {dialect!r}，"
            f"已支持: {sorted(_DIALECT_MIGRATIONS)}")
    with engine.begin() as conn:
        migrate(conn, insp)
