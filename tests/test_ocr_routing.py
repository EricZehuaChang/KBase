from kbase.db import make_session_factory
from kbase.ingest.pipeline import IngestPipeline, pdf_has_text_layer
from kbase.models import Document, KnowledgeBase
from kbase.plugins.base import OCRResult, OCRUnavailable
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import ChromaStore


def _scanned_pdf(path):
    """生成无文本层 PDF（纯图片页）。"""
    from PIL import Image
    img = Image.new("RGB", (600, 800), "white")
    img.save(path, "PDF")


class FakeOCR:
    def __init__(self, result=None, fail=False):
        self._result = result or OCRResult(markdown="# 扫描件\n识别出的内容。", confidence=0.9)
        self._fail = fail

    def to_markdown(self, path):
        if self._fail:
            raise OCRUnavailable("service down")
        return self._result


def _pipeline(tmp_path, fake_embedder, ocr):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    return factory, IngestPipeline(
        factory, StructureChunker(chunk_size=200, chunk_overlap=0),
        fake_embedder, ChromaStore(persist_dir=str(tmp_path / "c")),
        tmp_path / "f", ocr_backend=ocr)


def test_pdf_text_layer_detection(tmp_path):
    scanned = tmp_path / "s.pdf"
    _scanned_pdf(scanned)
    assert pdf_has_text_layer(scanned) is False


def test_scanned_pdf_routes_to_ocr(tmp_path, fake_embedder):
    factory, pipeline = _pipeline(tmp_path, fake_embedder, FakeOCR())
    f = tmp_path / "scan.pdf"
    _scanned_pdf(f)
    doc_id = pipeline.ingest_file("kb1", f, "scan.pdf")
    with factory() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == "ready"
        assert doc.ocr_confidence == 0.9


def test_image_routes_to_ocr(tmp_path, fake_embedder):
    from PIL import Image
    factory, pipeline = _pipeline(tmp_path, fake_embedder, FakeOCR())
    f = tmp_path / "pic.png"
    Image.new("RGB", (100, 100), "white").save(f)
    doc_id = pipeline.ingest_file("kb1", f, "pic.png")
    with factory() as s:
        assert s.get(Document, doc_id).status == "ready"


def test_ocr_unavailable_sets_pending(tmp_path, fake_embedder):
    factory, pipeline = _pipeline(tmp_path, fake_embedder, FakeOCR(fail=True))
    f = tmp_path / "scan.pdf"
    _scanned_pdf(f)
    doc_id = pipeline.ingest_file("kb1", f, "scan.pdf")
    with factory() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == "pending_ocr"        # 非 failed


def test_no_ocr_backend_scanned_fails_with_message(tmp_path, fake_embedder):
    factory, pipeline = _pipeline(tmp_path, fake_embedder, None)
    f = tmp_path / "scan.pdf"
    _scanned_pdf(f)
    doc_id = pipeline.ingest_file("kb1", f, "scan.pdf")
    with factory() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == "failed"
        assert "OCR" in doc.error
