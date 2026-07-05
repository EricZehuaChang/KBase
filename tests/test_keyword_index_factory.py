"""make_keyword_index 工厂：按方言选择 FTS5（SQLite）或 PGKeywordIndex（PG）。
api/main.py、reindex.py 等构造 KeywordIndex 的地方都改走这个工厂，
避免散落各处的 if dialect == ... 分支。"""
import pytest

from kbase.index.factory import make_keyword_index
from kbase.index.keyword import KeywordIndex
from kbase.index.keyword_pg import PGKeywordIndex


def test_sqlite_dialect_selects_fts5_keyword_index():
    idx = make_keyword_index(lambda: None, dialect="sqlite")
    assert isinstance(idx, KeywordIndex)


def test_postgresql_dialect_selects_pg_keyword_index():
    idx = make_keyword_index(lambda: None, dialect="postgresql")
    assert isinstance(idx, PGKeywordIndex)


def test_unsupported_dialect_raises():
    with pytest.raises(ValueError, match="mysql"):
        make_keyword_index(lambda: None, dialect="mysql")
