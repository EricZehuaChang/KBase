"""结构分块：沿 Markdown 标题切父块（章节），父块内按长度切叶子块。

chunk_size 按字符计。纯中文下与 token 数接近，但混合中英文/数字/表格时，
同字符数的 token 数可能显著更高（512 字符可能达 700-1000+ token），
下游 embedding 层不得按 token 假设。

表格感知（M6 表格最终版）：Markdown 表格是私有化 RAG 的头号质量雷区——
通用长度切分会把表头行和数据行切进不同块，叶子块沦为 "| 350 | 500 |"
这类无语义裸行，向量与 BM25 双路全废。本分块器对表格做三件事：
1. **原子性**：表格独立成块，绝不与普通文本混切、绝不从中间切开；
2. **大表按行组切**：超过 chunk_size 的表按数据行分组，**每组重复表头**
   （保住"列名↔值"绑定），组大小按字符预算折算；
3. **行线性化**：每个表格块生成 "表头=值" 句子化文本存入
   meta["layout"]={kind:"table", linearized:...}——检索两路用线性化文本
   （见 kbase/embed_text.py），块正文保留原始 Markdown 供 LLM 阅读。
"""
import re
import uuid

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from kbase.plugins.base import ChunkData
from kbase.plugins.registry import registry

_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]

# 两种表格来源：markitdown（Word/Excel/Markdown）产出 Markdown 管道表格；
# GLM-OCR（扫描件）产出 HTML <table>——两者都要走表格感知分块，否则扫描件
# 表格（客户最常见场景）会退化成普通文本被切碎。
# Markdown 表格：表头行 + 分隔行(|---|:---:等) + 数据行若干。行首可有缩进。
_MD_TABLE_RE = re.compile(
    r"^[ \t]*\|.*\|[ \t]*\n[ \t]*\|[ \t:\-|]+\|[ \t]*\n(?:[ \t]*\|.*\|[ \t]*\n?)*",
    re.MULTILINE)
# HTML 表格：<table>...</table>（GLM-OCR layout_parsing 的表格产出形态）。
_HTML_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _split_cells(row: str) -> list[str]:
    """拆一行 Markdown 表格的单元格：去掉首尾管道后按 | 切，转义管道 \\| 先保护。"""
    protected = row.strip().strip("|").replace("\\|", "\x00")
    return [c.replace("\x00", "|").strip() for c in protected.split("|")]


def _parse_html_table(html: str) -> tuple[list[str], list[list[str]]] | None:
    """解析 HTML <table> → (表头, 数据行)。首行 <tr> 作表头（GLM-OCR 输出
    首行即列名，不区分 th/td）；单元格内嵌标签剥除、空白折叠。"""
    trs = _TR_RE.findall(html)
    if not trs:
        return None

    def cells(tr: str) -> list[str]:
        return [re.sub(r"\s+", " ", _TAG_RE.sub("", c)).strip()
                for c in _CELL_RE.findall(tr)]

    header = cells(trs[0])
    rows = [cells(tr) for tr in trs[1:]]
    return (header, rows) if header else None


def parse_table(table: str) -> tuple[list[str], list[list[str]]] | None:
    """解析表格（Markdown 管道 或 HTML <table>）→ (表头, 数据行列表)；
    形状不合法返回 None（调用方按普通文本处理，绝不因解析失败丢内容）。"""
    if "<table" in table.lower():
        return _parse_html_table(table)
    lines = [ln for ln in table.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    header = _split_cells(lines[0])
    rows = [_split_cells(ln) for ln in lines[2:]]        # lines[1] 是分隔行
    return (header, rows) if header else None


def linearize_table(header: list[str], rows: list[list[str]]) -> str:
    """行线性化："列1=值1；列2=值2。" 每行一句。列数不齐时按较短侧截断
    （真实文档的残缺行常见，宁可少一列也不错位）。空单元格跳过。"""
    out = []
    for row in rows:
        pairs = [f"{h}={v}" for h, v in zip(header, row) if v]
        if pairs:
            out.append("；".join(pairs) + "。")
    return "\n".join(out)


def _table_markdown(header: list[str], rows: list[list[str]]) -> str:
    head = "| " + " | ".join(header) + " |"
    sep = "|" + "|".join([" --- "] * len(header)) + "|"
    body = ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join([head, sep, *body])


def split_table(md_table: str, chunk_size: int) -> list[tuple[str, str]]:
    """表格 → [(块正文Markdown, 线性化文本)]。整表在预算内则单块；超预算按
    数据行分组，每组**重复表头**。返回空列表=解析失败（按普通文本回退）。"""
    parsed = parse_table(md_table)
    if parsed is None:
        return []
    header, rows = parsed
    if not rows:
        text = _table_markdown(header, rows)
        return [(text, linearize_table(header, rows) or text)]
    # 每组行数按字符预算折算：至少 1 行，预算减掉表头开销
    row_chars = max(1, sum(len("| " + " | ".join(r) + " |") + 1 for r in rows) // len(rows))
    header_chars = len(header) * 8 + 20
    per_group = max(1, (chunk_size - header_chars) // row_chars)
    out = []
    for i in range(0, len(rows), per_group):
        group = rows[i:i + per_group]
        out.append((_table_markdown(header, group), linearize_table(header, group)))
    return out


# 跨页断表合并时允许"跨越"的装饰性短文本段：纯页码/分隔符（如"— 3 —"、
# "第 4 页 共 12 页"）。真实文字段（哪怕很短的说明句）会阻断合并——宁可
# 不合并也不能把两张真正独立的表错拼成一张。
_PAGE_NOISE_RE = re.compile(r"^[\s\-—–_·.。,，、/\\|()（）\d页第共之]*$")


def merge_split_tables(segments: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """跨页断表合并（GLM-OCR 多页 PDF 的头号问题）：分页会把一张表切成
    两个相邻 <table>，第二段通常**没有表头**——不合并的话第二段首行数据会
    被误当表头，线性化键值全部错位。

    合并条件（保守，防误拼独立表）：相邻表格段（中间无内容或只有页码类
    装饰行）且**列数相同**。第二段首行与第一段表头相同 → 视为续页重复
    表头，去重；不同 → 视为延续数据行，全部并入。合并产物统一重建为
    Markdown 管道表格。三页以上连续断表按同规则链式合并。"""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(segments):
        kind, content = segments[i]
        if kind != "table":
            out.append(segments[i])
            i += 1
            continue
        parsed = parse_table(content)
        if parsed is None:
            out.append(segments[i])
            i += 1
            continue
        header, rows = parsed
        merged = False
        j = i + 1
        while j < len(segments):
            nkind, ncontent = segments[j]
            if nkind == "text":
                t = ncontent.strip()
                # 页码噪声可跨越（合并成功时随之丢弃——它本就不该进知识块）
                if len(t) <= 12 and _PAGE_NOISE_RE.match(t):
                    j += 1
                    continue
                break
            nparsed = parse_table(ncontent)
            if nparsed is None or len(nparsed[0]) != len(header):
                break
            nheader, nrows = nparsed
            if nheader == header:
                rows = rows + nrows              # 续页重复表头：去重合并
            else:
                rows = rows + [nheader] + nrows  # 续页无表头：首行也是数据
            merged = True
            j += 1
        if merged:
            out.append(("table", _table_markdown(header, rows)))
            i = j
        else:
            out.append(segments[i])
            i += 1
    return out


def split_segments(text: str) -> list[tuple[str, str]]:
    """把章节文本切成 [(kind, content)]，kind ∈ {"text", "table"}。
    表格边界由 Markdown/HTML 两种表格正则共同确定（合并、按位置排序），
    表格之间/前后的普通文本原样保序。"""
    spans = [(m.start(), m.end(), m.group(0))
             for m in _MD_TABLE_RE.finditer(text)]
    spans += [(m.start(), m.end(), m.group(0))
              for m in _HTML_TABLE_RE.finditer(text)]
    spans.sort()
    segments: list[tuple[str, str]] = []
    pos = 0
    for start, end, table in spans:
        if start < pos:          # 防御：两正则理论上不重叠，重叠则跳过后者
            continue
        before = text[pos:start]
        if before.strip():
            segments.append(("text", before))
        segments.append(("table", table))
        pos = end
    tail = text[pos:]
    if tail.strip():
        segments.append(("text", tail))
    return segments


@registry.register("chunker", "structure")
class StructureChunker:
    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 64):
        if chunk_overlap >= chunk_size:
            raise ValueError(
                f"chunk_overlap ({chunk_overlap}) 必须小于 chunk_size ({chunk_size})，"
                "请检查配置 chunker.chunk_size / chunker.chunk_overlap")
        # 公开属性：供 kb 级参数覆盖时读取默认值（见 ingest/pipeline.py 的
        # _chunker_for），避免耦合 langchain splitter 的私有属性
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self._header_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=_HEADERS, strip_headers=True
        )
        self._text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", "。", "；", " ", ""],
        )

    def chunk(self, markdown: str, doc_name: str) -> list[ChunkData]:
        out: list[ChunkData] = []
        for section in self._header_splitter.split_text(markdown):
            titles = [section.metadata[key] for key in ("h1", "h2", "h3", "h4")
                      if key in section.metadata]
            heading_path = " > ".join([doc_name, *titles])
            parent = ChunkData(id=str(uuid.uuid4()), text=section.page_content,
                               heading_path=heading_path)
            out.append(parent)
            # 短于 chunk_size 的章节会产生文本相同的父块+单叶子块，属预期设计
            # （叶子用于向量检索，父块用于上下文组装），勿"优化"合并。
            leaves: list[ChunkData] = []
            for kind, content in merge_split_tables(
                    split_segments(section.page_content)):
                if kind == "table":
                    pieces = split_table(content, self.chunk_size)
                    if pieces:
                        for body, linearized in pieces:
                            leaves.append(ChunkData(
                                id=str(uuid.uuid4()), text=body,
                                heading_path=heading_path, parent_id=parent.id,
                                meta={"layout": {"kind": "table",
                                                 "linearized": linearized}}))
                        continue
                    # 表格解析失败：回退为普通文本切分，内容绝不丢
                for p in self._text_splitter.split_text(content):
                    leaves.append(ChunkData(id=str(uuid.uuid4()), text=p,
                                            heading_path=heading_path,
                                            parent_id=parent.id))
            for i, leaf in enumerate(leaves):
                leaf.prev_id = leaves[i - 1].id if i > 0 else None
                leaf.next_id = leaves[i + 1].id if i < len(leaves) - 1 else None
            out.extend(leaves)
        return out
