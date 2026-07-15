"""文档内嵌图片：提取（摄取时）与回答富化（citations 附图）。

多模态回答第一期（图片）的两个动作：
1. extract_pdf_images：文本层 PDF 摄取成功后，用 pypdf 提取各页内嵌图片
   → 落盘 files/{doc_id}/images/ + document_images 行（按页关联）。
   小图过滤（logo/分隔线/图标噪声）：宽高任一 < 64px 或字节数 < 5KB 跳过。
2. attach_images：citations 建好后按 (doc_id, page) 查图——命中某页的
   引用自动带上该页插图，前端在答案下方渲染缩略图。**图片不进 LLM
   prompt**（零幻觉风险）：模型只答文字，媒体由检索命中的事实关联给出。

设计边界（如实注明）：只覆盖文本层 PDF（页码可定位）。docx/md 无页概念、
OCR 路径无逐页产物，附图会洪泛或无从关联，留给后续按 caption 级关联再做。
"""
import io
import logging
import uuid
from pathlib import Path

from kbase.models import DocumentImage

logger = logging.getLogger(__name__)

MIN_DIMENSION = 64          # 宽或高小于该值视为图标/装饰，跳过（主过滤器）
# 字节线只兜"尺寸元数据虚标"的极端件：纯色块示意图 PNG 压缩率很高
# （640x200 流程图可低至 ~3KB），5KB 会误杀有效插图，取 2KB。
MIN_BYTES = 2 * 1024
MAX_IMAGES_PER_DOC = 200    # 防御异常 PDF（每页塞几百张小图）撑爆磁盘


def extract_pdf_images(sf, doc_id: str, pdf_path, images_dir) -> int:
    """从文本层 PDF 提取内嵌图片。返回落库张数；解析失败抛异常由调用方
    决定是否吞（pipeline 侧包 try/except——提图失败不阻塞摄取）。"""
    from PIL import Image
    from pypdf import PdfReader

    images_dir = Path(images_dir)
    reader = PdfReader(str(pdf_path))
    rows: list[DocumentImage] = []
    saved = 0
    for page_no, page in enumerate(reader.pages, start=1):
        for img in page.images:
            if saved >= MAX_IMAGES_PER_DOC:
                logger.warning("doc %s 图片超过 %d 张上限，其余跳过",
                               doc_id, MAX_IMAGES_PER_DOC)
                break
            data = img.data
            if len(data) < MIN_BYTES:
                continue
            try:
                pil = Image.open(io.BytesIO(data))
                width, height = pil.size
                fmt = (pil.format or "PNG").lower()
            except Exception:  # noqa: BLE001 —— 解不开的内嵌对象直接跳过
                continue
            if width < MIN_DIMENSION or height < MIN_DIMENSION:
                continue
            ext = "jpg" if fmt in ("jpeg", "jpg") else fmt
            filename = f"p{page_no}-{saved + 1}.{ext}"
            images_dir.mkdir(parents=True, exist_ok=True)
            (images_dir / filename).write_bytes(data)
            rows.append(DocumentImage(id=str(uuid.uuid4()), doc_id=doc_id,
                                      page=page_no, filename=filename,
                                      width=width, height=height))
            saved += 1
    if rows:
        with sf() as s:
            s.add_all(rows)
            s.commit()
    return saved


_DOCX_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff")


def extract_docx_images(sf, doc_id: str, docx_path, images_dir) -> int:
    """从 docx 提取插图并做 **caption 级锚定**：docx 无页概念，按文档流
    追踪"当前标题"（w:pStyle Heading*/纯数字样式，或带 w:outlineLvl 的
    大纲段落），图片锚到它出现时所在的章节标题——回答引用命中该章节
    （heading ∈ citation.heading_path）时附图。page 存 0 哨兵区分 PDF 行。"""
    import xml.etree.ElementTree as ET
    import zipfile

    from PIL import Image

    images_dir = Path(images_dir)
    rows: list[DocumentImage] = []
    saved = 0
    with zipfile.ZipFile(str(docx_path)) as zf:
        # rId → word/media/xxx 映射
        rels_xml = zf.read("word/_rels/document.xml.rels")
        rid_to_media = {
            rel.get("Id"): rel.get("Target")
            for rel in ET.fromstring(rels_xml)
            if "image" in (rel.get("Type") or "")}
        doc_root = ET.fromstring(zf.read("word/document.xml"))

        current_heading = ""
        for para in doc_root.iter(f"{{{_DOCX_NS['w']}}}p"):
            # 标题判定：pStyle 以 Heading 开头（英文模板）/纯数字（中文
            # Word 样式 id 惯例），或段属性里带 outlineLvl（最可靠信号）
            ppr = para.find(f"{{{_DOCX_NS['w']}}}pPr")
            is_heading = False
            if ppr is not None:
                style = ppr.find(f"{{{_DOCX_NS['w']}}}pStyle")
                val = style.get(f"{{{_DOCX_NS['w']}}}val", "") if style is not None else ""
                outline = ppr.find(f"{{{_DOCX_NS['w']}}}outlineLvl")
                is_heading = (val.startswith("Heading") or val.isdigit()
                              or outline is not None)
            text = "".join(t.text or "" for t in
                           para.iter(f"{{{_DOCX_NS['w']}}}t")).strip()
            if is_heading and text:
                current_heading = text
            # 本段里的图片（w:drawing → a:blip @r:embed）
            for blip in para.iter(f"{{{_DOCX_NS['a']}}}blip"):
                if saved >= MAX_IMAGES_PER_DOC:
                    break
                rid = blip.get(f"{{{_DOCX_NS['r']}}}embed")
                media = rid_to_media.get(rid)
                if not media:
                    continue
                member = "word/" + media.lstrip("/")
                try:
                    data = zf.read(member)
                except KeyError:
                    continue
                if len(data) < MIN_BYTES:
                    continue
                try:
                    pil = Image.open(io.BytesIO(data))
                    width, height = pil.size
                    fmt = (pil.format or "PNG").lower()
                except Exception:  # noqa: BLE001
                    continue
                if width < MIN_DIMENSION or height < MIN_DIMENSION:
                    continue
                ext = "jpg" if fmt in ("jpeg", "jpg") else fmt
                saved += 1
                filename = f"h{saved}.{ext}"
                images_dir.mkdir(parents=True, exist_ok=True)
                (images_dir / filename).write_bytes(data)
                rows.append(DocumentImage(
                    id=str(uuid.uuid4()), doc_id=doc_id, page=0,
                    heading=(current_heading or None), filename=filename,
                    width=width, height=height))
    if rows:
        with sf() as s:
            s.add_all(rows)
            s.commit()
    return saved


def attach_images(sf, citations: list[dict]) -> None:
    """原地富化 citations，三路锚点：
    1. PDF：(doc_id, page) 精确匹配该页插图；
    2. docx：page=0 行按 heading ∈ citation.heading_path（caption 级）；
    3. 图片文件文档（扫描照/截图上传）：命中即附原图本身（inline 直链）。
    没有任何锚点线索的引用不附——宁缺勿滥不洪泛。"""
    doc_ids = {c["doc_id"] for c in citations if c.get("doc_id")}
    if not doc_ids:
        return
    with sf() as s:
        rows = (s.query(DocumentImage)
                .filter(DocumentImage.doc_id.in_(doc_ids)).all())
    by_page: dict = {}
    by_doc_headed: dict = {}
    for r in rows:
        entry = {"url": f"/api/documents/{r.doc_id}/images/{r.filename}",
                 "name": r.filename, "width": r.width, "height": r.height}
        if r.page:
            by_page.setdefault((r.doc_id, r.page), []).append(entry)
        elif r.heading:
            by_doc_headed.setdefault(r.doc_id, []).append((r.heading, entry))
    for c in citations:
        doc_id = c.get("doc_id")
        if not doc_id:
            continue
        images = list(by_page.get((doc_id, c.get("page")), []))
        heading_path = c.get("heading_path") or ""
        for heading, entry in by_doc_headed.get(doc_id, []):
            if heading in heading_path and entry not in images:
                images.append(entry)
        # 图片文件文档：整个文件就是那张图，命中即回原图（用户拍板的
        # "命中图片已经足够"），复用 original 直链的 inline 形态
        doc_name = (c.get("doc_name") or "").lower()
        if not images and doc_name.endswith(_IMAGE_SUFFIXES):
            images.append({
                "url": f"/api/documents/{doc_id}/original?disposition=inline",
                "name": c.get("doc_name"), "width": 0, "height": 0})
        if images:
            c["images"] = images
