"""关键词索引共享分词器：jieba 预分词 + 空格连接。

线程安全说明：jieba 的词典加载是懒初始化，第一次调用 cut 类函数才
触发（约 0.3s）。摄取管道会在线程池里并行分词（上传 3 文件=3 线程首
次同时撞上初始化），旧版实测间歇性有文档卡在 processing——初始化锁虽
在，但首次初始化与并发 cut 存在时序窗口（2026-09-07 CI 全量跑暴露）。
对策：模块导入即同步 initialize()，把 0.3s 花在 import 而不是第一个
文档的处理里。

两种 KeywordIndex 后端（SQLite FTS5 与 PostgreSQL tsvector）都用同一份
中文分词结果——分词永远在应用层完成，PG 侧不装 zhparser 等中文扩展，
`to_tsvector('simple', ...)` 只是把预分词后的空格连接串按空格切成 lexeme，
与 FTS5 的 `tokenize='unicode61'`（同样只按空格切预分词结果）语义对齐。
"""
import jieba


def _tokenize(s: str) -> str:
    return " ".join(t.strip() for t in jieba.cut_for_search(s) if t.strip())
