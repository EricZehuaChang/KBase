"""文本层 PDF 结构化解析（opendataloader-pdf，veraPDF 系 Java 引擎，Apache-2.0）。

auto 路里文本层 PDF 的主解析器：输出带标题层级/阅读序/边框表格的 Markdown
和逐元素页码的 JSON。对比 markitdown 的 PDF 内核（pdfminer：平文本零标题、
伪表格），真实标题结构可直接喂标题感知分块（StructureChunker 依赖 h1-h6），
页码定位改用与 Markdown 同源的 JSON 页文本，一致性优于 pdfminer 二次提取。

失败语义：不可用（未装包/无 Java 运行时/JVM 解析失败/空产物）一律返回
None，由 pipeline 回退 markitdown——升级失败模式等于回到升级前的现状，
不引入新单点。扫描件不进此模块（needs_ocr 已在上游分流到 GLM-OCR 路）。

环境开关：KBASE_PDF_PARSER=markitdown 强制走旧路（灰度/排障用）。

已知边界（实测华勤 SGS/CTI 报告样本得出）：
- 无边框表格识别不出，会降级为按阅读序的连续文本（数据仍在、检索可命中，
  只是不进表格感知分块）；需要精确表格仍用 parse_mode="ocr" 表格增强路。
- CJK 断行空格：PDF 文本层按行存储，行合并会在中文字符间引入空格
  （"监 管平台"），本模块统一剔除，保护 FTS5 精确匹配。
"""
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# 只删【CJK 与 CJK 之间】的水平空白：不碰换行（保 Markdown 结构）、不碰
# 西文单词间距（"RPA 平台" 这类中西混排的空格属正常排版，保留）。
# 覆盖：CJK 统一表意文字（含扩展A/兼容区）、CJK 标点、全角标点。
_CJK = r"[㐀-䶿一-鿿豈-﫿　-〿！-｠]"
_CJK_GAP = re.compile(f"(?<={_CJK})[ \t]+(?={_CJK})")


def strip_cjk_gaps(text: str) -> str:
    """剔除 CJK 字符间的断行残留空格（纯函数，可直接单测）。"""
    return _CJK_GAP.sub("", text)


def odl_available() -> bool:
    """三重探测：环境开关未禁用 + 包已安装 + java 在 PATH。任一不满足即
    走 markitdown 旧路，调用方无需区分原因。"""
    if os.environ.get("KBASE_PDF_PARSER", "").lower() == "markitdown":
        return False
    try:
        import opendataloader_pdf  # noqa: F401 —— 仅探测可导入
    except ImportError:
        return False
    return shutil.which("java") is not None


def parse_pdf(path) -> tuple[str, list[str]] | None:
    """解析文本层 PDF，返回 (markdown, 逐页文本) 或 None（回退信号）。

    逐页文本从 JSON 的元素 page number 聚合而来，供 locate_chunk_pages
    做叶子块页码匹配；与 Markdown 同一次解析产出，文本形态一致。
    image_output="off"：插图提取由既有 doc_images（pypdf 路）负责落库，
    这里不产图避免临时目录垃圾与重复。
    """
    if not odl_available():
        return None
    import json

    import opendataloader_pdf
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # 单文件调用：摄取按文档粒度进来，JVM 冷启 1~2s 相比解析收益
            # 可接受；批量导入场景 bulk_import 也是逐文档走同一管道。
            opendataloader_pdf.convert(
                input_path=str(path), output_dir=tmp,
                format="markdown,json", image_output="off", quiet=True)
            files = list(Path(tmp).iterdir())
            md = next((f for f in files if f.suffix == ".md"), None)
            js = next((f for f in files if f.suffix == ".json"), None)
            if md is None:
                return None
            markdown = md.read_text(encoding="utf-8")
            page_texts: list[str] = []
            if js is not None:
                doc = json.loads(js.read_text(encoding="utf-8"))
                n = int(doc.get("number of pages") or 0)
                if n > 0:
                    page_texts = [""] * n
                    _collect_page_texts(doc.get("kids", []), page_texts)
    except Exception as e:  # noqa: BLE001 —— 任何解析故障都回退旧路，不阻塞摄取
        logger.warning("opendataloader 解析失败，回退 markitdown: %s", e)
        return None
    markdown = _normalize_degenerate(strip_cjk_gaps(markdown))
    if not markdown.strip():
        return None
    return markdown, page_texts


def _normalize_degenerate(markdown: str) -> str:
    """退化产物防线：整份 markdown 只有标题行（无任何正文/表格）时，摘掉
    标题记号降级为普通段落。

    版式分析对"每页仅一行大字"的封面型/海报型 PDF 会把全部内容判成标题；
    纯标题喂 StructureChunker 得到零叶子块（标题只进 heading_path 不成块），
    整文不可检索——比 markitdown 平文本还差。降级为段落后行为与旧路对齐。
    正常文档（有正文或表格）原样返回，标题层级完整保留。"""
    lines = [line for line in markdown.splitlines() if line.strip()]
    if not lines or not all(line.lstrip().startswith("#") for line in lines):
        return markdown
    return "\n\n".join(line.lstrip().lstrip("#").strip() for line in lines)


def _collect_page_texts(nodes, page_texts: list[str]) -> None:
    """递归聚合各页文本。JSON 元素嵌套键有三种：kids（普通容器/单元格内容）、
    rows/cells（表格结构），统一下钻，schema 局部变化不致漏采。"""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        content = node.get("content")
        page = node.get("page number")
        if content and isinstance(page, int) and 1 <= page <= len(page_texts):
            page_texts[page - 1] += str(content) + "\n"
        for key in ("kids", "rows", "cells"):
            sub = node.get(key)
            if isinstance(sub, list):
                _collect_page_texts(sub, page_texts)
