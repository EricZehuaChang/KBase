"""KeywordIndex 工厂：按 SQLAlchemy engine 方言名选择实现。

sqlite -> KeywordIndex（SQLite FTS5 版，kbase/index/keyword.py）
postgresql -> PGKeywordIndex（PostgreSQL tsvector+GIN 版，
              kbase/index/keyword_pg.py）

api/main.py、reindex.py 等构造 KeywordIndex 实例的地方统一走这里，
不在各处散落 if dialect == ... 分支。"""
from kbase.index.keyword import KeywordIndex
from kbase.index.keyword_pg import PGKeywordIndex

_DIALECT_IMPLS = {
    "sqlite": KeywordIndex,
    "postgresql": PGKeywordIndex,
}


def make_keyword_index(session_factory, dialect: str):
    try:
        impl = _DIALECT_IMPLS[dialect]
    except KeyError:
        raise ValueError(
            f"make_keyword_index: 不支持的方言 {dialect!r}，"
            f"已支持: {sorted(_DIALECT_IMPLS)}")
    return impl(session_factory)
