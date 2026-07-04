"""摄取：文件 → markitdown/OCR → 标准 Markdown → 分块 → [可选]上下文增强 → 叶子块向量化 → 入库。
单文件失败只标记该文档，不向外抛异常（批次隔离）。"""
import hashlib
import json
import uuid
from pathlib import Path

from kbase.models import Chunk, Document, KnowledgeBase
from kbase.plugins.base import Chunker, Embedder, OCRUnavailable, VectorStore
from kbase.plugins.chunkers.structure import StructureChunker

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def pdf_has_text_layer(path, sample_pages: int = 3, min_chars_per_page: int = 50) -> bool:
    """采样前 N 页，平均每页文本字符数达到阈值即认为有文本层（非扫描件）。"""
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    pages = reader.pages[:sample_pages]
    if not pages:
        return False
    chars = sum(len((p.extract_text() or "").strip()) for p in pages)
    return chars / len(pages) >= min_chars_per_page


class IngestPipeline:
    def __init__(self, session_factory, chunker: Chunker, embedder: Embedder,
                 store: VectorStore, files_dir: Path, keyword_index=None,
                 enricher=None, ocr_backend=None):
        self._sf = session_factory
        self._chunker = chunker
        self._embedder = embedder
        self._store = store
        self._files_dir = Path(files_dir)
        self._keyword_index = keyword_index
        self._enricher = enricher
        self._ocr = ocr_backend

    def ingest_file(self, kb_id: str, path: Path, original_name: str) -> str:
        content_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        with self._sf() as s:
            dup = s.query(Document).filter_by(
                kb_id=kb_id, content_hash=content_hash).first()
            if dup:
                return dup.id
            doc = Document(id=str(uuid.uuid4()), kb_id=kb_id,
                           filename=original_name, content_hash=content_hash,
                           status="parsing", source_path=str(path))
            s.add(doc)
            s.commit()
            doc_id = doc.id
        self._run(kb_id, doc_id, path, original_name)
        return doc_id

    def retry_document(self, doc_id: str) -> None:
        """对既有文档（通常 pending_ocr/failed）重跑解析。用 Document.source_path
        找回原始文件，不依赖上传目录反查（上传文件名含 uuid 前缀无法直接推导）。"""
        with self._sf() as s:
            doc = s.get(Document, doc_id)
            if doc is None:
                return
            kb_id, path, name = doc.kb_id, doc.source_path, doc.filename
        if not path or not Path(path).exists():
            self._set_status(doc_id, "failed", error="原始文件已丢失，无法重试")
            return
        self._set_status(doc_id, "parsing")
        self._run(kb_id, doc_id, Path(path), name)

    def _run(self, kb_id: str, doc_id: str, path: Path, name: str) -> None:
        try:
            ocr_confidence = self._process(kb_id, doc_id, path, name)
            self._set_status(doc_id, "ready", ocr_confidence=ocr_confidence)
        except OCRUnavailable:
            # OCR 服务暂时不可达/超时：可重试，不是文档本身的问题
            self._set_status(doc_id, "pending_ocr")
        except Exception as e:  # noqa: BLE001 —— 批次隔离，失败落库
            self._set_status(doc_id, "failed", error=f"{type(e).__name__}: {e}")

    def _process(self, kb_id: str, doc_id: str, path: Path, name: str):
        suffix = Path(path).suffix.lower()
        needs_ocr = suffix in _IMAGE_EXTS or (
            suffix == ".pdf" and not pdf_has_text_layer(path))
        ocr_confidence = None
        if needs_ocr:
            if self._ocr is None:
                raise ValueError("扫描件/图片需要 OCR，当前未配置 OCR 后端")
            result = self._ocr.to_markdown(path)      # OCRUnavailable 向上抛，由 _run 捕获
            markdown = result.markdown
            ocr_confidence = result.confidence
        else:
            from markitdown import MarkItDown
            markdown = MarkItDown(enable_plugins=False).convert(str(path)).text_content
        if not markdown.strip():
            raise ValueError("解析结果为空（可能是扫描件，未正确路由到 OCR）")
        # markitdown 对不认识/损坏的二进制文件会静默降级为纯文本转换器，
        # 逐字节解码后返回"成功"但含有控制字符的乱码（而不是抛异常）。
        # 真实文档解析结果不应包含 NUL 等 C0 控制符（\t\n\r 除外），
        # 出现即视为损坏文件，主动判失败，不让乱码进入分块/向量化。
        if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in markdown):
            raise ValueError("解析结果包含控制字符，疑似损坏或不受支持的文件格式")
        # 双存：Markdown 中间产物落盘，重建索引不用重新解析
        out_dir = self._files_dir / doc_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "content.md").write_text(markdown, encoding="utf-8")

        kb_config = self._load_kb_config(kb_id)
        chunker = self._chunker_for(kb_config)
        chunks = chunker.chunk(markdown, doc_name=name)
        leaves = [c for c in chunks if c.parent_id is not None]

        if leaves and self._enricher is not None and kb_config.get("enrich", {}).get("enabled"):
            leaves = self._enricher.enrich(name, markdown, leaves)

        if leaves:
            # 只嵌入叶子块；父块仅存 SQLite 供上下文组装。
            # 超长文本会被 embedding 模型静默截断，叶子块 512 字符远低于上限。
            # enrich_context（若有）作为前缀参与向量化，帮助稠密检索理解片段
            # 在全文中的定位；lstrip 去掉无增强时开头多余的换行。
            vectors = self._embedder.embed(
                [f"{c.meta.get('enrich_context', '')}\n{c.heading_path}\n{c.text}".lstrip()
                 for c in leaves])
            self._store.upsert(
                collection=kb_id,
                ids=[c.id for c in leaves],
                vectors=vectors,
                metas=[{"doc_id": doc_id, "parent_id": c.parent_id}
                       for c in leaves],
            )
        with self._sf() as s:
            for c in chunks:
                s.add(Chunk(id=c.id, doc_id=doc_id, kb_id=kb_id,
                            parent_id=c.parent_id, prev_id=c.prev_id,
                            next_id=c.next_id, heading_path=c.heading_path,
                            text=c.text, is_leaf=c.parent_id is not None,
                            enrich_context=c.meta.get("enrich_context")))
            s.commit()

        if self._keyword_index and leaves:
            # 关键词索引保持索引原始文本（heading_path+text，无 enrich 前缀）：
            # 增强句是"这段话在讲什么"的摘要，有利于语义向量匹配同义表达；
            # 但关键词检索要的是文中原样出现的词/编号（如文件号），加增强前缀
            # 反而会稀释 BM25 对原文关键词的权重，因此这里刻意不用增强后的文本。
            self._keyword_index.index(
                kb_id, [(c.id, doc_id, f"{c.heading_path}\n{c.text}") for c in leaves])

        return ocr_confidence

    def _load_kb_config(self, kb_id: str) -> dict:
        with self._sf() as s:
            kb = s.get(KnowledgeBase, kb_id)
        if kb is None or not kb.config:
            return {}
        try:
            return json.loads(kb.config)
        except (json.JSONDecodeError, TypeError):
            return {}

    def _chunker_for(self, kb_config: dict) -> Chunker:
        chunk_size = kb_config.get("chunk_size")
        chunk_overlap = kb_config.get("chunk_overlap")
        if chunk_size is None and chunk_overlap is None:
            return self._chunker
        # kb 级覆盖：只在配置里显式给了 chunk_size/chunk_overlap 时才新建 chunker，
        # 缺省的那一项沿用默认 chunker 的公开构造参数
        if isinstance(self._chunker, StructureChunker):
            return StructureChunker(
                chunk_size=chunk_size if chunk_size is not None else self._chunker.chunk_size,
                chunk_overlap=chunk_overlap if chunk_overlap is not None else self._chunker.chunk_overlap)
        return self._chunker

    def _set_status(self, doc_id: str, status: str, error: str | None = None,
                    ocr_confidence: float | None = None):
        with self._sf() as s:
            doc = s.get(Document, doc_id)
            if doc is None:      # 文档已被删除等竞态情况：静默跳过，保住"绝不抛异常"契约
                return
            doc.status = status
            doc.error = error
            if ocr_confidence is not None:
                doc.ocr_confidence = ocr_confidence
            s.commit()
