"""文本层 PDF 主解析器切换（opendataloader）：CJK 空格清洗纯函数、可用性
探测与回退契约、真实 JVM 解析（需 Java，缺失自动跳过）、端到端摄取回退。

回退契约是本轮升级的安全底线：opendataloader 任何形式不可用时，摄取行为
必须与升级前（markitdown 路）完全一致。"""
import pytest

from kbase.ingest import pdf_odl
from kbase.ingest.pdf_odl import odl_available, parse_pdf, strip_cjk_gaps
from tests.test_citation_page import _text_pdf


# ---------------- strip_cjk_gaps 纯函数 ----------------


def test_strip_cjk_gaps_removes_linebreak_artifacts():
    """PDF 文本层行合并在中文字符间引入的空格必须剔除（华勤实测："监 管平台"）。"""
    assert strip_cjk_gaps("海关智慧监 管平台") == "海关智慧监管平台"
    assert strip_cjk_gaps("项目需求 书八、培训要求") == "项目需求书八、培训要求"


def test_strip_cjk_gaps_keeps_western_and_mixed_spacing():
    """西文单词间距、中西文之间的排版空格属正常内容，不得误删。"""
    assert strip_cjk_gaps("hello world") == "hello world"
    assert strip_cjk_gaps("凌动RPA 产品与 AI 能力") == "凌动RPA 产品与 AI 能力"


def test_strip_cjk_gaps_preserves_newlines_and_markdown():
    """只删水平空白：换行承载 Markdown 结构（标题/表格行），必须原样保留。"""
    text = "## 培训 目标\n\n|列 一|列 二|"
    assert strip_cjk_gaps(text) == "## 培训目标\n\n|列一|列二|"


def test_strip_cjk_gaps_handles_fullwidth_punctuation():
    """全角括号/顿号与汉字之间的断行空格同样要清（属 CJK 排版单元）。"""
    assert strip_cjk_gaps("（响应招标 文件）") == "（响应招标文件）"
    assert strip_cjk_gaps("铅、 镉、汞") == "铅、镉、汞"


def test_normalize_degenerate_heading_only_markdown():
    """全标题无正文的退化产物必须降级为段落（否则零叶子块不可检索）；
    正常文档（标题+正文）原样保留层级。"""
    from kbase.ingest.pdf_odl import _normalize_degenerate
    degenerate = "# Cover title line\n\n## Another heading line\n"
    assert _normalize_degenerate(degenerate) == \
        "Cover title line\n\nAnother heading line"
    normal = "# 标题\n\n正文段落。\n"
    assert _normalize_degenerate(normal) == normal


# ---------------- 可用性探测与回退契约 ----------------


def test_env_switch_forces_markitdown(monkeypatch):
    """KBASE_PDF_PARSER=markitdown 是灰度/排障的硬开关，必须优先于一切探测。"""
    monkeypatch.setenv("KBASE_PDF_PARSER", "markitdown")
    assert odl_available() is False
    assert parse_pdf("whatever.pdf") is None


def test_parse_pdf_returns_none_when_unavailable(monkeypatch):
    """包缺失/无 Java 时 parse_pdf 返回 None（回退信号），绝不抛异常。"""
    monkeypatch.setattr(pdf_odl, "odl_available", lambda: False)
    assert parse_pdf("whatever.pdf") is None


def test_pipeline_falls_back_to_markitdown(tmp_path, fake_embedder, monkeypatch):
    """端到端回退：强制走旧路时，文本层 PDF 摄取行为与升级前一致
    （ready + 页码定位仍由 pdfminer 路提供）。"""
    monkeypatch.setenv("KBASE_PDF_PARSER", "markitdown")
    from tests.test_citation_page import PAGE1, PAGE2, _client
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "回退库"}).json()["id"]
    pdf = tmp_path / "fallback.pdf"
    _text_pdf(pdf, [PAGE1, PAGE2])
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("fallback.pdf", pdf.read_bytes(), "application/pdf"))])
    doc = c.get(f"/api/kb/{kb_id}/documents").json()[0]
    assert doc["status"] == "ready", doc["error"]
    blocks = c.post(f"/api/kb/{kb_id}/search",
                    json={"query": "housing subsidy standard", "top_k": 5}).json()["blocks"]
    assert blocks and blocks[0]["page"] == 1


# ---------------- 真实 JVM 解析（需 Java 11+，缺失自动跳过） ----------------

needs_java = pytest.mark.skipif(
    not odl_available(), reason="opendataloader 不可用（未装包或无 Java），跳过真实解析")


@needs_java
def test_parse_pdf_real_markdown_and_page_texts(tmp_path):
    """真实解析双页文本层 PDF：markdown 含两页正文，逐页文本按页归位
    （供 locate_chunk_pages 的同源页文本契约）。"""
    pdf = tmp_path / "two-pages.pdf"
    _text_pdf(pdf, ["Alpha housing subsidy content page",
                    "Beta travel expense content page"])
    parsed = parse_pdf(pdf)
    assert parsed is not None
    markdown, page_texts = parsed
    assert "Alpha housing subsidy" in markdown
    assert "Beta travel expense" in markdown
    assert len(page_texts) == 2
    assert "Alpha housing subsidy" in page_texts[0]
    assert "Beta travel expense" in page_texts[1]
    # 页文本不得串页：第 2 页内容不出现在第 1 页聚合里
    assert "Beta travel expense" not in page_texts[0]


@needs_java
def test_pdf_ingest_via_odl_end_to_end(tmp_path, fake_embedder):
    """主路端到端：opendataloader 激活时文本层 PDF 摄取 ready，检索块
    携带正确页码（页码来自 JSON 同源页文本，而非 pdfminer 二次提取）。"""
    from tests.test_citation_page import PAGE1, PAGE2, _client
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "主路库"}).json()["id"]
    pdf = tmp_path / "odl.pdf"
    _text_pdf(pdf, [PAGE1, PAGE2])
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("odl.pdf", pdf.read_bytes(), "application/pdf"))])
    doc = c.get(f"/api/kb/{kb_id}/documents").json()[0]
    assert doc["status"] == "ready", doc["error"]
    b1 = c.post(f"/api/kb/{kb_id}/search",
                json={"query": "housing subsidy standard", "top_k": 5}).json()["blocks"]
    assert b1 and b1[0]["page"] == 1
    b2 = c.post(f"/api/kb/{kb_id}/search",
                json={"query": "hotel rates approved ceiling destination",
                      "top_k": 5}).json()["blocks"]
    assert b2 and b2[0]["page"] == 2
