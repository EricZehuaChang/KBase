"""摄取：文件 → markitdown → 标准 Markdown → 分块 → 叶子块向量化 → 入库。
单文件失败只标记该文档，不向外抛异常（批次隔离）。"""
import hashlib
import uuid
from pathlib import Path

from kbase.models import Chunk, Document
from kbase.plugins.base import Chunker, Embedder, VectorStore


class IngestPipeline:
    def __init__(self, session_factory, chunker: Chunker, embedder: Embedder,
                 store: VectorStore, files_dir: Path):
        self._sf = session_factory
        self._chunker = chunker
        self._embedder = embedder
        self._store = store
        self._files_dir = Path(files_dir)

    def ingest_file(self, kb_id: str, path: Path, original_name: str) -> str:
        content_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        with self._sf() as s:
            dup = s.query(Document).filter_by(
                kb_id=kb_id, content_hash=content_hash).first()
            if dup:
                return dup.id
            doc = Document(id=str(uuid.uuid4()), kb_id=kb_id,
                           filename=original_name, content_hash=content_hash,
                           status="parsing")
            s.add(doc)
            s.commit()
            doc_id = doc.id
        try:
            self._process(kb_id, doc_id, path, original_name)
            self._set_status(doc_id, "ready")
        except Exception as e:  # noqa: BLE001 —— 批次隔离，失败落库
            self._set_status(doc_id, "failed", error=f"{type(e).__name__}: {e}")
        return doc_id

    def _process(self, kb_id: str, doc_id: str, path: Path, name: str):
        from markitdown import MarkItDown
        markdown = MarkItDown(enable_plugins=False).convert(str(path)).text_content
        if not markdown.strip():
            raise ValueError("解析结果为空（可能是扫描件，M1 不支持 OCR）")
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

        chunks = self._chunker.chunk(markdown, doc_name=name)
        leaves = [c for c in chunks if c.parent_id is not None]
        if leaves:
            # 只嵌入叶子块；父块仅存 SQLite 供上下文组装。
            # 超长文本会被 embedding 模型静默截断，叶子块 512 字符远低于上限。
            vectors = self._embedder.embed(
                [f"{c.heading_path}\n{c.text}" for c in leaves])
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
                            text=c.text, is_leaf=c.parent_id is not None))
            s.commit()

    def _set_status(self, doc_id: str, status: str, error: str | None = None):
        with self._sf() as s:
            doc = s.get(Document, doc_id)
            doc.status = status
            doc.error = error
            s.commit()
