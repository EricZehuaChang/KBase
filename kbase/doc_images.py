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

MIN_DIMENSION = 64          # 宽或高小于该值视为图标/装饰，跳过
MIN_BYTES = 5 * 1024        # 过小的图多为线条/logo
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


def attach_images(sf, citations: list[dict]) -> None:
    """原地富化 citations：有 doc_id+page 的引用附上该页图片清单
    （url 指向 GET /api/documents/{doc_id}/images/{filename}）。
    无 page 的引用（docx/md/OCR 路径/老消息）不附——宁缺勿滥。"""
    keys = {(c["doc_id"], c["page"]) for c in citations
            if c.get("doc_id") and c.get("page")}
    if not keys:
        return
    doc_ids = {k[0] for k in keys}
    with sf() as s:
        rows = (s.query(DocumentImage)
                .filter(DocumentImage.doc_id.in_(doc_ids)).all())
    by_key: dict = {}
    for r in rows:
        by_key.setdefault((r.doc_id, r.page), []).append(
            {"url": f"/api/documents/{r.doc_id}/images/{r.filename}",
             "name": r.filename, "width": r.width, "height": r.height})
    for c in citations:
        images = by_key.get((c.get("doc_id"), c.get("page")))
        if images:
            c["images"] = images
