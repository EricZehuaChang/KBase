"""多模态回答一期（图片）：文本层 PDF 摄取提图落库、回答 citations 按页
附图、图片直链端点与路径穿越防护、小图过滤。"""
import io
import zlib  # noqa: F401 —— 构造器保留压缩流扩展位

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM


def _jpeg_bytes(w: int, h: int, color=(200, 60, 60)) -> bytes:
    buf = io.BytesIO()
    # noise 填充避免 JPEG 压得太小掉到 MIN_BYTES 过滤线以下
    img = Image.frombytes(
        "RGB", (w, h),
        bytes((i * 37 + j * 11) % 256 for j in range(h) for i in range(w * 3)))
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def build_pdf(text: str, images: list[bytes], sizes: list[tuple[int, int]]) -> bytes:
    """手工构造单页文本层 PDF，内嵌若干 DCTDecode JPEG（无需 reportlab）。"""
    content = f"BT /F1 14 Tf 72 720 Td ({text}) Tj ET\n"
    for i in range(len(images)):
        w, h = sizes[i]
        content += f"q {w} 0 0 {h} 72 {600 - i * 180} cm /Im{i + 1} Do Q\n"
    content_b = content.encode()

    xobjs = " ".join(f"/Im{i + 1} {5 + i} 0 R" for i in range(len(images)))
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         f"/Resources << /Font << /F1 4 0 R >> /XObject << {xobjs} >> >> "
         f"/Contents {5 + len(images)} 0 R >>").encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, (jpeg, (w, h)) in enumerate(zip(images, sizes)):
        objs.append(
            (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
             f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
             f"/Length {len(jpeg)} >>\nstream\n").encode() + jpeg + b"\nendstream")
    objs.append(b"<< /Length %d >>\nstream\n" % len(content_b) + content_b
                + b"\nendstream")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objs) + 1))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objs) + 1, xref_pos))
    return out.getvalue()


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def test_pdf_images_extracted_and_attached_to_citations(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    # 一大一小两张图：大图入库，小图（48px）被过滤
    # 文本需 >=50 字符（pdf_has_text_layer 的每页阈值），否则被判成扫描件
    pdf = build_pdf("Housing subsidy standard is 800 yuan per month for staff over two years",
                    [_jpeg_bytes(320, 240), _jpeg_bytes(48, 48)],
                    [(320, 240), (48, 48)])
    r = c.post(f"/api/kb/{kb}/documents",
               files=[("files", ("subsidy.pdf", pdf, "application/pdf"))])
    assert r.status_code == 200
    docs = c.get(f"/api/kb/{kb}/documents").json()
    assert docs[0]["status"] == "ready", docs[0]
    doc_id = docs[0]["id"]

    # 检索：命中第 1 页的块应附该页图片（且只有过滤后的 1 张大图）
    hits = c.post(f"/api/kb/{kb}/search",
                  json={"query": "housing subsidy 800", "top_k": 5}).json()["blocks"]
    assert hits

    # 问答 citations 附图（走 _run_query 富化路径）
    import json as _json
    citations = []
    with c.stream("POST", f"/api/kb/{kb}/query",
                  json={"question": "housing subsidy standard"}) as resp:
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event == "citations":
                citations = _json.loads(line[5:].strip())
                break
    assert citations and citations[0]["page"] == 1
    images = citations[0].get("images")
    assert images and len(images) == 1, "大图附上、48px 小图被过滤"
    assert images[0]["width"] == 320

    # 图片直链可取回，且与提取产物一致
    img = c.get(images[0]["url"])
    assert img.status_code == 200
    assert img.headers["content-type"].startswith("image/")
    assert len(img.content) > 5 * 1024

    # 路径穿越防护
    assert c.get(f"/api/documents/{doc_id}/images/..%2F..%2Fcontent.md").status_code == 404
    assert c.get(f"/api/documents/{doc_id}/images/nope.png").status_code == 404


def test_md_docs_get_no_images(tmp_path, fake_embedder):
    """无页概念的文档（md）不附图——宁缺勿滥，不做全文档洪泛。"""
    import json as _json
    c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb}/documents",
           files=[("files", ("补贴.md", "# 补贴\n住房补贴满两年可申领。".encode("utf-8"),
                             "text/markdown"))])
    citations = []
    with c.stream("POST", f"/api/kb/{kb}/query",
                  json={"question": "住房补贴怎么申领"}) as resp:
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event == "citations":
                citations = _json.loads(line[5:].strip())
                break
    assert citations
    assert all("images" not in ci for ci in citations)
