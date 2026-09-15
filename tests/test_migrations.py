from sqlalchemy import inspect, text

from kbase.db import make_session_factory
from kbase.migrations import run_migrations
from kbase.models import ApiKey


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
        # T18 导入批次是新表：create_all 自动建（不需要 _COLUMN_MIGRATIONS 登记），
        # 这里钉住"老库升级后也有这张表"——没有它，批次接口在老部署上直接 500。
        assert "import_batches" in tables
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


def test_migration_creates_unique_index_on_documents(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        insp = inspect(s.get_bind())
        idx_names = {ix["name"] for ix in insp.get_indexes("documents")}
        assert "uq_doc_kb_hash" in idx_names


def test_migration_dedups_legacy_duplicate_rows_before_index(tmp_path):
    """模拟历史库里已有重复 (kb_id, content_hash) 行（M1/M2 未加约束时摄取产生）：
    迁移需要先幂等删除重复行（保留 created_at 最早的一条），再建唯一索引，
    否则 CREATE UNIQUE INDEX 本身就会因既有重复数据失败。"""
    import sqlite3
    db = tmp_path / "dup.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE knowledge_bases (id VARCHAR(36) PRIMARY KEY, name VARCHAR(200), created_at DATETIME);
        CREATE TABLE documents (id VARCHAR(36) PRIMARY KEY, kb_id VARCHAR(36), filename VARCHAR(500),
            content_hash VARCHAR(64), status VARCHAR(20), error TEXT, created_at DATETIME);
        CREATE TABLE chunks (id VARCHAR(36) PRIMARY KEY, doc_id VARCHAR(36), kb_id VARCHAR(36),
            parent_id VARCHAR(36), prev_id VARCHAR(36), next_id VARCHAR(36),
            heading_path TEXT, text TEXT, is_leaf BOOLEAN);
    """)
    conn.execute(
        "INSERT INTO documents (id, kb_id, filename, content_hash, status, created_at) "
        "VALUES ('d1','kb1','a.md','hash1','ready','2026-01-01 00:00:00')")
    conn.execute(
        "INSERT INTO documents (id, kb_id, filename, content_hash, status, created_at) "
        "VALUES ('d2','kb1','a-dup.md','hash1','ready','2026-01-02 00:00:00')")
    conn.execute(
        "INSERT INTO documents (id, kb_id, filename, content_hash, status, created_at) "
        "VALUES ('d3','kb1','b.md','hash2','ready','2026-01-01 00:00:00')")
    conn.commit()
    conn.close()

    factory = make_session_factory(f"sqlite:///{db}")
    with factory() as s:
        rows = s.execute(text(
            "SELECT id FROM documents WHERE kb_id='kb1' AND content_hash='hash1'")).all()
        assert [r[0] for r in rows] == ["d1"]     # 保留最早一条，重复行已被清理
        remaining = {r[0] for r in s.execute(text("SELECT id FROM documents")).all()}
        assert remaining == {"d1", "d3"}
        insp = inspect(s.get_bind())
        idx_names = {ix["name"] for ix in insp.get_indexes("documents")}
        assert "uq_doc_kb_hash" in idx_names

    # 再跑一次迁移（幂等）：不应报错，索引仍在
    factory2 = make_session_factory(f"sqlite:///{db}")
    with factory2() as s:
        insp = inspect(s.get_bind())
        idx_names = {ix["name"] for ix in insp.get_indexes("documents")}
        assert "uq_doc_kb_hash" in idx_names


def test_existing_api_keys_table_gets_t09_columns(tmp_path):
    """模拟 T09 之前的旧库：api_keys 只有原来的 8 列（含 scope_kb_ids），
    没有任何有效期/最近使用/IP 白名单/配额/停用列。迁移后六列补齐，且
    **存量行一律 NULL**——新列不设 DEFAULT（SQLite ALTER 也回填不了），
    "不限"语义由读取端解释：NULL=永不过期/从未使用/不限来源/不限流/未停用
    （见 kbase/migrations.py 的注释与 kbase/ratelimit.py、auth/deps.py）。"""
    import sqlite3
    db = tmp_path / "pre-t09.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE api_keys (id VARCHAR(36) PRIMARY KEY, name VARCHAR(200),
            prefix VARCHAR(20), key_hash VARCHAR(64), role VARCHAR(20),
            revoked BOOLEAN, scope_kb_ids TEXT, created_at DATETIME);
    """)
    conn.execute(
        "INSERT INTO api_keys (id, name, prefix, key_hash, role, revoked,"
        " scope_kb_ids, created_at) VALUES ('k1', '旧 Key', 'abcd1234', 'hash1',"
        " 'viewer', 0, NULL, '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()

    factory = make_session_factory(f"sqlite:///{db}")
    with factory() as s:
        insp = inspect(s.get_bind())
        cols = {c["name"] for c in insp.get_columns("api_keys")}
        assert {"expires_at", "last_used_at", "ip_allow", "rpm",
                "daily_quota", "disabled"} <= cols
        row = s.execute(text(
            "SELECT expires_at, last_used_at, ip_allow, rpm, daily_quota, disabled "
            "FROM api_keys WHERE id='k1'")).one()
        assert tuple(row) == (None, None, None, None, None, None)
        # 新表由 create_all 建（新表不需要列迁移条目）
        assert "api_key_usage_daily" in inspect(s.get_bind()).get_table_names()
        assert insp.get_pk_constraint("api_key_usage_daily")[
            "constrained_columns"] == ["key_id", "day"]

    # NULL 语义的读取端解释：未停用/未过期 → 仍可用（新库与老库同一个路径）
    with factory() as s:
        key = s.query(ApiKey).filter_by(id="k1").one()
        assert not key.disabled          # None → 未停用
        assert key.expires_at is None    # None → 永不过期
        assert key.rpm is None and key.daily_quota is None   # None → 不限流
        assert key.ip_allow is None      # None → 不限来源


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


def test_existing_conversations_table_gets_user_id_column(tmp_path):
    """模拟鉴权改造前（M5-1 之前）的旧库：conversations 表没有 user_id 列，
    迁移后应补上（列缺失守卫，见 _COLUMN_MIGRATIONS），且既有会话行迁移后
    该列取值为 NULL——不倒推补全归属人（见 kbase/conversations.py
    _visible_filter 的设计取舍）。"""
    import sqlite3
    db = tmp_path / "pre-m5-1.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE conversations (id VARCHAR(36) PRIMARY KEY, kb_id VARCHAR(36),
            title VARCHAR(200), created_at DATETIME, updated_at DATETIME);
    """)
    conn.execute(
        "INSERT INTO conversations (id, kb_id, title, created_at, updated_at) "
        "VALUES ('c1', 'kb1', '旧会话', '2026-01-01 00:00:00', '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()
    factory = make_session_factory(f"sqlite:///{db}")
    with factory() as s:
        insp = inspect(s.get_bind())
        assert "user_id" in {c["name"] for c in insp.get_columns("conversations")}
        got = s.execute(text("SELECT user_id FROM conversations WHERE id='c1'")).scalar()
        assert got is None


# ---- M4-2 H3：方言感知分支（纯逻辑单测，无需真实 PG 连接） -------------------
#
# run_migrations 内部按 engine.dialect.name 分派到 _run_sqlite_migrations /
# _run_postgresql_migrations。真连 PG 需要活的服务器（见 test_keyword_pg.py
# 的 @pytest.mark.pg 集成测试），这里只验证「选对了哪条路径、拼出了哪些
# DDL」——用一个假 engine/connection 记录被执行的 SQL 文本，不需要网络。

class _FakeDialect:
    def __init__(self, name):
        self.name = name


class _FakeConnection:
    def __init__(self):
        self.executed: list[str] = []

    def execute(self, stmt, *args, **kwargs):
        self.executed.append(str(stmt))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeInspector:
    def __init__(self, tables, columns):
        self._tables = tables
        self._columns = columns

    def get_table_names(self):
        return self._tables

    def get_columns(self, table):
        return [{"name": c} for c in self._columns.get(table, [])]


class _FakeEngine:
    """伪造 engine：只满足 run_migrations 用到的接口面
    （.dialect.name、.begin() 返回连接、inspect() 走 get_table_names/get_columns）。
    不建立任何真实网络连接，纯 Python 对象。"""
    def __init__(self, dialect_name, tables=(), columns=None):
        self.dialect = _FakeDialect(dialect_name)
        self._tables = list(tables)
        self._columns = columns or {}
        self.conn = _FakeConnection()

    def begin(self):
        return self.conn


def _patch_inspect(monkeypatch, engine):
    import kbase.migrations as mig
    fake_insp = _FakeInspector(engine._tables, engine._columns)
    monkeypatch.setattr(mig, "inspect", lambda e: fake_insp)


def test_run_migrations_sqlite_dialect_creates_fts5_virtual_table(monkeypatch):
    engine = _FakeEngine("sqlite", tables=["documents"],
                         columns={"documents": ["id", "kb_id", "content_hash",
                                                  "created_at"]})
    _patch_inspect(monkeypatch, engine)
    run_migrations(engine)
    sql = "\n".join(engine.conn.executed)
    assert "fts5" in sql.lower()
    assert "tsvector" not in sql.lower()


def test_run_migrations_postgresql_dialect_skips_fts5_and_uses_gin(monkeypatch):
    engine = _FakeEngine("postgresql", tables=["documents"],
                         columns={"documents": ["id", "kb_id", "content_hash",
                                                  "created_at"]})
    _patch_inspect(monkeypatch, engine)
    run_migrations(engine)
    sql = "\n".join(engine.conn.executed)
    assert "fts5" not in sql.lower()
    assert "tsvector" in sql.lower()
    assert "gin" in sql.lower()


def test_run_migrations_postgresql_dedup_uses_ctid_not_rowid(monkeypatch):
    """PostgreSQL 没有 rowid，去重查询须改用 ctid 做并列 tiebreaker。"""
    engine = _FakeEngine("postgresql", tables=["documents"],
                         columns={"documents": ["id", "kb_id", "content_hash",
                                                  "created_at"]})
    _patch_inspect(monkeypatch, engine)
    run_migrations(engine)
    sql = "\n".join(engine.conn.executed)
    assert "rowid" not in sql.lower()
    assert "ctid" in sql.lower()


def test_run_migrations_postgresql_column_guard_uses_inspector_columns(monkeypatch):
    """列缺失守卫复用 SQLAlchemy inspector（底层走 information_schema.columns），
    两种方言共用同一段判断逻辑，不需要方言专属的列探测代码。"""
    engine = _FakeEngine("postgresql", tables=["chunks"],
                         columns={"chunks": ["id", "doc_id", "kb_id"]})
    _patch_inspect(monkeypatch, engine)
    run_migrations(engine)
    sql = "\n".join(engine.conn.executed)
    assert "alter table chunks add column enrich_context" in sql.lower()


def test_run_migrations_postgresql_unique_index_ddl_is_pg_compatible(monkeypatch):
    engine = _FakeEngine("postgresql", tables=["documents"],
                         columns={"documents": ["id", "kb_id", "content_hash",
                                                  "created_at"]})
    _patch_inspect(monkeypatch, engine)
    run_migrations(engine)
    sql = "\n".join(engine.conn.executed)
    assert "create unique index if not exists uq_doc_kb_hash" in sql.lower()


def test_column_migration_types_are_dialect_safe():
    """T09 回归：迁移里的列类型必须在**两种方言**下都存在。

    真事故（2026-09-14）：T09 给 api_keys 加 expires_at/last_used_at 时写了
    SQLite 的 DATETIME，PG 没有这个类型，真 PG 上直接
    `UndefinedObject: type "datetime" does not exist`——表现为 PG 集成测试
    在 fixture 建表阶段整批 error。SQLite 测试全绿，只有真 PG 能发现。

    这里不需要数据库：列出每种方言下"必须能被接受"的类型名集合，逐个断言
    映射后的结果不落在方言禁用的名单里。DATETIME 是 PG 的禁用词，新增类型
    时若引入别的方言专有名字，把禁用词加进来即可。
    """
    from kbase.migrations import _COLUMN_MIGRATIONS, _ddl_type_for

    banned = {"postgresql": {"datetime"}, "sqlite": set()}
    for table, column, ddl_type in _COLUMN_MIGRATIONS:
        for dialect, forbidden in banned.items():
            rendered = _ddl_type_for(dialect, ddl_type)
            lowered = rendered.lower()
            for word in forbidden:
                assert word not in lowered, (
                    f"{dialect} 不支持类型 {word!r}："
                    f"{table}.{column} 声明为 {ddl_type!r}，"
                    f"映射后是 {rendered!r}——用 _DIALECT_TYPE_NAMES 补映射")
            assert rendered.strip(), f"{table}.{column} 的类型不能为空"


def test_datetime_maps_to_timestamp_on_postgres():
    """具体映射本身也钉住：DATETIME→TIMESTAMP（PG），sqlite 原样。"""
    from kbase.migrations import _ddl_type_for

    assert _ddl_type_for("postgresql", "DATETIME") == "TIMESTAMP"
    assert _ddl_type_for("sqlite", "DATETIME") == "DATETIME"
    # 带后缀的写法只替换类型词，不破坏后缀
    assert _ddl_type_for("postgresql", "INTEGER DEFAULT 0") == "INTEGER DEFAULT 0"
