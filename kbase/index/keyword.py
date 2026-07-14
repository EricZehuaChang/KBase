"""关键词索引：SQLite FTS5 + jieba。写入与查询用同一分词器，
FTS 侧 tokenize=unicode61 只按空格切预分词结果。

_tokenize 从 kbase.index.tokenize 导入（共享实现，PGKeywordIndex 同款），
两种后端分词语义保持一致，见该模块的模块级 docstring。"""
import jieba
from sqlalchemy import text

from kbase.index.tokenize import _tokenize
from kbase.plugins.base import Hit


def _fts_query(s: str) -> str:
    # 每个词加引号防 FTS 语法字符，OR 连接保召回（融合层负责排序）。
    # token 内若含字面双引号（如输入本身带引号），FTS5 用双写 "" 转义，
    # 否则拼出的 """ 会被解析成 unterminated string 语法错误。
    tokens = [t for t in jieba.cut_for_search(s) if t.strip()]
    return " OR ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tokens) if tokens else '""'


class KeywordIndex:
    def __init__(self, session_factory):
        self._sf = session_factory

    def index(self, kb_id: str, rows: list[tuple[str, str, str]]) -> None:
        """rows: [(chunk_id, doc_id, raw_text)]"""
        if not rows:
            return
        with self._sf() as s:
            for chunk_id, doc_id, raw in rows:
                s.execute(text(
                    "INSERT INTO chunks_fts(chunk_id, kb_id, doc_id, text) "
                    "VALUES (:c, :k, :d, :t)"),
                    {"c": chunk_id, "k": kb_id, "d": doc_id, "t": _tokenize(raw)})
            s.commit()

    def search(self, kb_id: str, query: str, top_k: int = 20) -> list[Hit]:
        """score = -bm25，无界、越大越好，仅用于路内排序；
        与稠密余弦不可比，融合走 RRF 按名次。"""
        with self._sf() as s:
            rows = s.execute(text(
                "SELECT chunk_id, bm25(chunks_fts) AS r FROM chunks_fts "
                "WHERE chunks_fts MATCH :q AND kb_id = :k "
                "ORDER BY r LIMIT :n"),
                {"q": _fts_query(query), "k": kb_id, "n": top_k}).fetchall()
        # bm25 越小越相关，取负转为越大越好
        return [Hit(chunk_id=r[0], score=-float(r[1]), meta={"route": "keyword"})
                for r in rows]

    def delete_ids(self, chunk_ids: list[str]) -> None:
        """按 chunk id 精确删除（M6-1 chunk 启停/编辑重索引前清旧行——
        index() 是纯 INSERT，不先删会留重复行）。"""
        if not chunk_ids:
            return
        with self._sf() as s:
            for cid in chunk_ids:
                s.execute(text("DELETE FROM chunks_fts WHERE chunk_id = :c"),
                          {"c": cid})
            s.commit()

    def delete_doc(self, doc_id: str) -> None:
        with self._sf() as s:
            s.execute(text("DELETE FROM chunks_fts WHERE doc_id = :d"),
                      {"d": doc_id})
            s.commit()

    def delete_kb(self, kb_id: str) -> None:
        with self._sf() as s:
            s.execute(text("DELETE FROM chunks_fts WHERE kb_id = :k"),
                      {"k": kb_id})
            s.commit()
