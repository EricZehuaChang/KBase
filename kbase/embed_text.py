"""嵌入/关键词索引文本组成的**唯一权威**（M6 表格版收敛）。

历史上这套组成逻辑散在三处（ingest/pipeline.py、reindex.py、chunk_admin.py），
靠注释互相提醒"三处同步演进"——表格线性化引入第四种变体后靠注释已不可靠，
全部收敛到本模块。任何调整嵌入文本组成的需求只改这里。

组成规则：
- 普通文本块：embed = enrich_context + heading_path + text（既有语义不变）；
  keyword = heading_path + text（不带 enrich 前缀，理由见 pipeline 原注释：
  增强句利于语义匹配、但会稀释 BM25 对原文关键词的权重）。
- 表格块（layout.kind == "table"）：embed/keyword 都用**行线性化文本**
  （"表头=值"句子，见 chunkers/structure.py linearize_table）——原始
  Markdown 表格保留在 chunk.text 里供 LLM 阅读与前端展示，但对检索两路，
  线性化文本才是可命中的语义/词面形态（裸表格行如 "| 350 | 500 |" 对
  向量与 BM25 都近乎噪声）。
"""
import json


def _layout_dict(layout) -> dict:
    """layout 兼容三种来源：ChunkData.meta['layout']（dict）、
    Chunk.layout（JSON 字符串）、None。解析失败按无 layout 处理。"""
    if layout is None:
        return {}
    if isinstance(layout, dict):
        return layout
    try:
        return json.loads(layout) or {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _content_text(text: str, layout) -> str:
    ld = _layout_dict(layout)
    if ld.get("kind") == "table" and ld.get("linearized"):
        return ld["linearized"]
    return text


def embed_input(enrich_context: str | None, heading_path: str, text: str,
                layout=None) -> str:
    """向量化输入文本。lstrip 去掉无增强时开头多余换行（沿用既有行为）。"""
    return (f"{enrich_context or ''}\n{heading_path}\n"
            f"{_content_text(text, layout)}").lstrip()


def keyword_input(heading_path: str, text: str, layout=None) -> str:
    """关键词索引输入文本（无 enrich 前缀）。"""
    return f"{heading_path}\n{_content_text(text, layout)}"
