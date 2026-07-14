"""关键词索引：PostgreSQL tsvector + GIN，jieba 预分词。

与 kbase/index/keyword.py（SQLite FTS5 版）同契约（index/search/delete_doc/
delete_kb），供 kbase/index/factory.py 按方言选择。分词复用共享的
kbase.index.tokenize._tokenize——两种后端的中文切词语义保持一致，PG 侧
不装 zhparser 等中文全文检索扩展，`to_tsvector('simple', ...)` 只是把
预分词后的空格连接串按空格切成 lexeme，等价于 FTS5 的
tokenize='unicode61'（同样只按空格切预分词结果）。

后备表 chunks_kw（chunk_id/kb_id/doc_id/tsv）与 GIN 索引由
kbase/migrations.py 的 PostgreSQL 分支创建，本模块只管读写。
"""
from sqlalchemy import text

from kbase.index.tokenize import _tokenize
from kbase.plugins.base import Hit


def _to_query(s: str) -> str:
    """把 jieba 分词结果拼成 to_tsquery 的 OR 表达式：'词1 | 词2 | ...'。

    与 keyword.py 的 _fts_query（FTS5 版，OR 连接保召回）同语义——不能用
    plainto_tsquery/websearch_to_tsquery，两者对空格分隔的多词一律按 AND
    语义连接，自然语言问句（jieba 切出的虚词多）几乎总能被至少一个虚词
    拖累到 0 命中，这正是本函数要修的 bug。

    每个 token 用单引号包起来当 tsquery 的 lexeme 字面量（而非裸写进表达式），
    避免 token 本身含 to_tsquery 语法保留字符（& | ! ( ) : ' 等——理论上
    jieba 分词结果不含这些符号，但不能假设输入永远干净）；token 内的字面
    单引号按 SQL 字符串规则双写转义。空 query 时退化为空字符串（不匹配
    任何行，与 FTS5 版 _fts_query 对空输入返回不可能匹配的 '""' 同构）。
    """
    tokens = [t for t in _tokenize(s).split(" ") if t]
    if not tokens:
        return ""
    return " | ".join(f"'{t.replace(chr(39), chr(39) * 2)}'" for t in tokens)


class PGKeywordIndex:
    def __init__(self, session_factory):
        self._sf = session_factory

    def index(self, kb_id: str, rows: list[tuple[str, str, str]]) -> None:
        """rows: [(chunk_id, doc_id, raw_text)]。chunk_id 为主键，
        对同一 chunk_id 重复调用按 upsert 语义覆盖（幂等，reindex 场景需要）。"""
        if not rows:
            return
        with self._sf() as s:
            for chunk_id, doc_id, raw in rows:
                s.execute(text(
                    "INSERT INTO chunks_kw(chunk_id, kb_id, doc_id, tsv) "
                    "VALUES (:c, :k, :d, to_tsvector('simple', :t)) "
                    "ON CONFLICT (chunk_id) DO UPDATE SET "
                    "kb_id = EXCLUDED.kb_id, doc_id = EXCLUDED.doc_id, "
                    "tsv = EXCLUDED.tsv"),
                    {"c": chunk_id, "k": kb_id, "d": doc_id, "t": _tokenize(raw)})
            s.commit()

    def search(self, kb_id: str, query: str, top_k: int = 20) -> list[Hit]:
        """score = ts_rank，无界、越大越好——与 FTS5 版的 -bm25 同为
        "越大越好"语义，两者都仅用于路内排序，融合走 RRF 按名次，
        不跨路直接比较分数。

        用 to_tsquery + 应用层拼好的 OR 表达式（_to_query），不用
        plainto_tsquery/websearch_to_tsquery——两者对多词查询一律 AND
        语义连接，自然语言问句（jieba 分词后虚词多）几乎总会被至少一个
        虚词拖累到 0 命中，与 keyword.py 的 FTS5 OR 语义不对等，会让
        PG 部署下的混合检索静默退化成纯稠密路。"""
        q = _to_query(query)
        if not q:
            return []
        with self._sf() as s:
            rows = s.execute(text(
                "SELECT chunk_id, ts_rank(tsv, to_tsquery('simple', :q)) AS r "
                "FROM chunks_kw "
                "WHERE tsv @@ to_tsquery('simple', :q) AND kb_id = :k "
                "ORDER BY r DESC LIMIT :n"),
                {"q": q, "k": kb_id, "n": top_k}).fetchall()
        return [Hit(chunk_id=r[0], score=float(r[1]), meta={"route": "keyword"})
                for r in rows]

    def delete_ids(self, chunk_ids: list[str]) -> None:
        """按 chunk id 精确删除（M6-1 chunk 启停；与 FTS5 版语义一致）。
        PG 版 index() 本身是 upsert，编辑重索引不依赖先删，但停用必须删行。"""
        if not chunk_ids:
            return
        with self._sf() as s:
            for cid in chunk_ids:
                s.execute(text("DELETE FROM chunks_kw WHERE chunk_id = :c"),
                          {"c": cid})
            s.commit()

    def delete_doc(self, doc_id: str) -> None:
        with self._sf() as s:
            s.execute(text("DELETE FROM chunks_kw WHERE doc_id = :d"),
                      {"d": doc_id})
            s.commit()

    def delete_kb(self, kb_id: str) -> None:
        with self._sf() as s:
            s.execute(text("DELETE FROM chunks_kw WHERE kb_id = :k"),
                      {"k": kb_id})
            s.commit()
