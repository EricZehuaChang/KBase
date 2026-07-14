"""引用定位（M5-2）：叶子块 → 源 PDF 页码的匹配、检索/引用链路透传、
原始文件 inline 预览的白名单。"""
import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from kbase.ingest.pipeline import locate_chunk_pages
from kbase.plugins.base import ChunkData
from tests.test_api import CFG, FakeLLM


def _leaf(text: str) -> ChunkData:
    return ChunkData(id=text[:8], text=text, heading_path="doc", parent_id="p1")


# ---------------- locate_chunk_pages 纯函数 ----------------


def test_locate_pages_basic_and_order():
    pages = ["第一章 住房补贴标准 每晚不超过五百元",
             "第二章 差旅规定 市内交通费包干"]
    leaves = [_leaf("第一章 住房补贴标准"), _leaf("第二章 差旅规定")]
    locate_chunk_pages(pages, leaves)
    assert leaves[0].meta["page"] == 1
    assert leaves[1].meta["page"] == 2


def test_locate_pages_forward_only_prevents_backtrack():
    """块序与页序一致：后面的块不回头匹配更早的页——页眉/套话在多页重复时，
    回头匹配会把后面的块错标到第一次出现的页。"""
    pages = ["通用条款 甲方乙方", "通用条款 甲方乙方 第二页专属内容"]
    leaves = [_leaf("第二页专属内容"), _leaf("通用条款 甲方乙方")]
    locate_chunk_pages(pages, leaves)
    assert leaves[0].meta["page"] == 2
    assert leaves[1].meta["page"] == 2      # 从第2页起找，不回退到第1页


def test_locate_pages_not_found_leaves_meta_absent():
    leaves = [_leaf("完全不存在的内容XYZ")]
    locate_chunk_pages(["第一页文本"], leaves)
    assert "page" not in leaves[0].meta


def test_locate_pages_whitespace_insensitive():
    """PDF 提取文本的换行/空格与分块文本不一致是常态，匹配必须去空白。"""
    pages = ["住 房 补 贴\n申 领 条 件"]
    leaves = [_leaf("住房补贴申领条件")]
    locate_chunk_pages(pages, leaves)
    assert leaves[0].meta["page"] == 1


# ---------------- 端到端：文本层 PDF 摄取 → 检索块带页码 ----------------


def _text_pdf(path, page_texts: list[str]) -> None:
    """手工构造最小文本层 PDF（Helvetica/ASCII，xref 偏移精确计算）。
    避免引入 reportlab/fpdf 依赖——测试只需要 pdfminer 能逐页提取的合法 PDF。"""
    objs: list[bytes] = []
    n = len(page_texts)
    font_num = 2 + 2 * n + 1
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for i, text in enumerate(page_texts):
        content_num = 4 + 2 * i
        objs.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                     f"/Contents {content_num} 0 R "
                     f"/Resources << /Font << /F1 {font_num} 0 R >> >> >>").encode())
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(b"<< /Length " + str(len(stream)).encode()
                    + b" >>\nstream\n" + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF").encode()
    path.write_bytes(bytes(out))


PAGE1 = ("Housing subsidy standard section. Employees with two full years of "
         "continuous service may apply for the housing subsidy benefit. The "
         "standard amount is five hundred per month for tier one cities.")
PAGE2 = ("Travel expense chapter. Intra city transport costs are covered by a "
         "fixed daily allowance. Hotel rates must not exceed the approved "
         "ceiling for the destination city under any circumstance.")


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


@pytest.fixture
def pdf_kb(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    pdf = tmp_path / "policy.pdf"
    _text_pdf(pdf, [PAGE1, PAGE2])
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("policy.pdf", pdf.read_bytes(), "application/pdf"))])
    doc = c.get(f"/api/kb/{kb_id}/documents").json()[0]
    assert doc["status"] == "ready", doc["error"]
    return c, kb_id, doc["id"]


def test_pdf_ingest_assigns_pages_to_search_blocks(pdf_kb):
    """无标题 PDF 只有一个父块，top_k 去重后每次查询只回 1 个块——块的 page
    应跟随**命中叶子**：查第 1 页内容得 page=1，查第 2 页内容得 page=2。"""
    c, kb_id, _doc_id = pdf_kb
    b1 = c.post(f"/api/kb/{kb_id}/search",
                json={"query": "housing subsidy standard", "top_k": 5}).json()["blocks"]
    assert b1 and b1[0]["page"] == 1, f"got: {[(b['page'], b['snippet'][:30]) for b in b1]}"
    b2 = c.post(f"/api/kb/{kb_id}/search",
                json={"query": "hotel rates approved ceiling destination",
                      "top_k": 5}).json()["blocks"]
    assert b2 and b2[0]["page"] == 2, f"got: {[(b['page'], b['snippet'][:30]) for b in b2]}"


def test_query_citations_carry_page(pdf_kb):
    c, kb_id, _doc_id = pdf_kb
    import json as _json
    citations = []
    with c.stream("POST", f"/api/kb/{kb_id}/query",
                  json={"question": "housing subsidy standard", "top_k": 3}) as r:
        event = ""
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event == "citations":
                citations = _json.loads(line[5:].strip())
                break
    assert citations and all("page" in ci for ci in citations)
    assert any(ci["page"] in (1, 2) for ci in citations)


def test_original_inline_disposition_whitelist(pdf_kb, tmp_path):
    """PDF 允许 inline（浏览器内联预览+#page 跳页）；HTML 类即使请求 inline
    也强制 attachment（防存储型 XSS）。"""
    c, kb_id, doc_id = pdf_kb
    r = c.get(f"/api/documents/{doc_id}/original?disposition=inline")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("inline")
    assert r.headers["content-type"].startswith("application/pdf")
    # 默认仍是 attachment
    r2 = c.get(f"/api/documents/{doc_id}/original")
    assert r2.headers["content-disposition"].startswith("attachment")
    # 非白名单类型：markdown 属白名单，用 .docx 验证强制 attachment 路径
    docx = tmp_path / "x.docx"
    docx.write_bytes(b"PK\x03\x04fakedocx")
    # 直接构造一条 source_path 指向 docx 的文档记录，绕过真实解析
    from kbase.db import make_session_factory
    from kbase.config import load_config, resolve_db_url
    from kbase.models import Document
    cfg = load_config(tmp_path / "kbase.yaml")
    sf = make_session_factory(resolve_db_url(cfg))
    with sf() as s:
        s.add(Document(id="doc-docx", kb_id=kb_id, filename="x.docx",
                       content_hash="h", status="ready",
                       source_path=str(docx)))
        s.commit()
    r3 = c.get("/api/documents/doc-docx/original?disposition=inline")
    assert r3.status_code == 200
    assert r3.headers["content-disposition"].startswith("attachment")
