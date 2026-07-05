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
        不跨路直接比较分数。"""
        with self._sf() as s:
            rows = s.execute(text(
                "SELECT chunk_id, ts_rank(tsv, plainto_tsquery('simple', :q)) AS r "
                "FROM chunks_kw "
                "WHERE tsv @@ plainto_tsquery('simple', :q) AND kb_id = :k "
                "ORDER BY r DESC LIMIT :n"),
                {"q": _tokenize(query), "k": kb_id, "n": top_k}).fetchall()
        return [Hit(chunk_id=r[0], score=float(r[1]), meta={"route": "keyword"})
                for r in rows]

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
