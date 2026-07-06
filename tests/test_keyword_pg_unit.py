"""PGKeywordIndex 纯单测：不需要真实 PG 连接，验证拼出的 SQL 形状与
_tokenize 复用——真实 index/search/delete 全链路见 test_keyword_pg.py 的
@pytest.mark.pg 集成测试（需要 KBASE_TEST_PG_URL）。"""
from kbase.index.keyword_pg import PGKeywordIndex
from kbase.index.tokenize import _tokenize


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    """记录每次 execute 的 (sql文本, params)，不连真实数据库。"""
    def __init__(self, fetch_rows=None):
        self.calls: list[tuple[str, dict]] = []
        self._fetch_rows = fetch_rows or []
        self.committed = False

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params or {}))
        return _FakeResult(self._fetch_rows)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sf_factory(session):
    return lambda: session


def test_keyword_pg_module_reuses_shared_tokenize():
    """PG 版沿用与 FTS5 版同一份 jieba 分词实现，不是各自维护副本。"""
    import kbase.index.keyword_pg as kwpg
    assert kwpg._tokenize is _tokenize


def test_index_writes_to_chunks_kw_with_to_tsvector_simple():
    session = _FakeSession()
    idx = PGKeywordIndex(_sf_factory(session))
    idx.index("kb1", [("c1", "d1", "公务卡结算范围")])
    assert session.committed
    sql, params = session.calls[0]
    assert "chunks_kw" in sql
    assert "to_tsvector('simple'" in sql
    assert params["c"] == "c1" and params["k"] == "kb1" and params["d"] == "d1"
    assert params["t"] == _tokenize("公务卡结算范围")


def test_index_empty_rows_is_noop():
    session = _FakeSession()
    idx = PGKeywordIndex(_sf_factory(session))
    idx.index("kb1", [])
    assert session.calls == []
    assert not session.committed


def test_index_upsert_on_conflict_chunk_id():
    """reindex 场景需要幂等覆盖，不是每次都新插入报唯一约束错误。"""
    session = _FakeSession()
    idx = PGKeywordIndex(_sf_factory(session))
    idx.index("kb1", [("c1", "d1", "text")])
    sql, _ = session.calls[0]
    assert "ON CONFLICT (chunk_id)" in sql
    assert "DO UPDATE" in sql


def test_search_uses_to_tsquery_or_join_and_ts_rank_desc():
    """Bug 1 修复：不能用 plainto_tsquery/websearch_to_tsquery——两者对
    多词查询都是 AND 语义连接，自然语言问句（jieba 分词后虚词多）几乎
    总会被至少一个虚词拖累到 0 命中。改用 to_tsquery + 应用层拼好的
    ' | ' OR 表达式（_to_query），与 FTS5 版 _fts_query 的 OR 语义对齐。"""
    session = _FakeSession(fetch_rows=[("c3", 0.5)])
    idx = PGKeywordIndex(_sf_factory(session))
    hits = idx.search("kb1", "公务卡的结算范围", top_k=3)
    sql, params = session.calls[0]
    assert "plainto_tsquery" not in sql
    assert "websearch_to_tsquery" not in sql
    assert "to_tsquery('simple'" in sql
    assert "ts_rank" in sql
    assert "ORDER BY r DESC" in sql
    assert " | " in params["q"]        # OR 连接，不是 AND
    tokens = _tokenize("公务卡的结算范围").split(" ")
    assert params["q"] == " | ".join(f"'{t}'" for t in tokens)
    assert params["k"] == "kb1" and params["n"] == 3
    assert hits == [type(hits[0])(chunk_id="c3", score=0.5, meta={"route": "keyword"})]


def test_search_empty_query_returns_no_hits_without_querying_db():
    """query 分词后为空（如纯空白/纯停用符号）时，_to_query 返回空字符串，
    search 应直接短路返回 []，不应该拼出空的 to_tsquery('simple', '') 送进
    数据库（那会是语法错误或匹配空结果集的低效查询）。"""
    session = _FakeSession(fetch_rows=[("c1", 0.5)])
    idx = PGKeywordIndex(_sf_factory(session))
    hits = idx.search("kb1", "   ", top_k=3)
    assert hits == []
    assert session.calls == []


def test_search_score_is_positive_and_higher_is_better():
    """score 语义与 FTS5 的负 bm25 一致：都是"越大越好"，只是量纲不同
    （ts_rank 天然为正，bm25 取负后也为正）——融合层按名次做 RRF，不跨路
    比较绝对分数，因此两种后端只需各自内部保持"越大越好"单调性。"""
    session = _FakeSession(fetch_rows=[("c1", 0.9), ("c2", 0.1)])
    idx = PGKeywordIndex(_sf_factory(session))
    hits = idx.search("kb1", "q", top_k=2)
    assert hits[0].score > hits[1].score > 0


def test_delete_doc_targets_doc_id():
    session = _FakeSession()
    idx = PGKeywordIndex(_sf_factory(session))
    idx.delete_doc("d1")
    sql, params = session.calls[0]
    assert "DELETE FROM chunks_kw" in sql and "doc_id" in sql
    assert params == {"d": "d1"}
    assert session.committed


def test_delete_kb_targets_kb_id():
    session = _FakeSession()
    idx = PGKeywordIndex(_sf_factory(session))
    idx.delete_kb("kb1")
    sql, params = session.calls[0]
    assert "DELETE FROM chunks_kw" in sql and "kb_id" in sql
    assert params == {"k": "kb1"}
    assert session.committed
