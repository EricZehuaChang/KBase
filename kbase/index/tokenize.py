"""关键词索引共享分词器：jieba 预分词 + 空格连接。

两种 KeywordIndex 后端（SQLite FTS5 与 PostgreSQL tsvector）都用同一份
中文分词结果——分词永远在应用层完成，PG 侧不装 zhparser 等中文扩展，
`to_tsvector('simple', ...)` 只是把预分词后的空格连接串按空格切成 lexeme，
与 FTS5 的 `tokenize='unicode61'`（同样只按空格切预分词结果）语义对齐。
"""
import jieba


def _tokenize(s: str) -> str:
    return " ".join(t.strip() for t in jieba.cut_for_search(s) if t.strip())
