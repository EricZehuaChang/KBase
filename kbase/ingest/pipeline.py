"""摄取：文件 → markitdown/OCR → 标准 Markdown → 分块 → [可选]上下文增强 → 叶子块向量化 → 入库。
单文件失败只标记该文档，不向外抛异常（批次隔离）。"""
import hashlib
import json
import logging
import uuid
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from kbase.embed_text import embed_input, keyword_input
from kbase.models import Chunk, Document, KnowledgeBase
from kbase.plugins.base import Chunker, Embedder, OCRUnavailable, VectorStore
from kbase.plugins.chunkers.structure import StructureChunker

logger = logging.getLogger(__name__)

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


def _pdf_page_texts(path) -> list[str]:
    """逐页提取 PDF 文本（pdfminer，与 markitdown 的 PDF 解析同引擎，
    文本形态一致度最高）。供引用定位的页码匹配用。"""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer
    pages: list[str] = []
    for layout in extract_pages(str(path)):
        pages.append("".join(el.get_text() for el in layout
                             if isinstance(el, LTTextContainer)))
    return pages


def locate_chunk_pages(page_texts: list[str], leaves, prefix_chars: int = 30) -> None:
    """引用定位（M5-2）：把每个叶子块映射到源 PDF 页码，写进 leaf.meta["page"]。

    匹配策略：取叶子文本去空白后的前 prefix_chars 个字符，在同样去空白的
    逐页文本里顺序查找，命中即记页码（1 起）。从上一次命中的页开始向后找
    ——块顺序与页序一致，避免重复内容（页眉/条款套话）误匹配到更早的页。
    找不到置 None（尽力而为：定位失败不影响检索与问答，只是前端少一个
    "第N页"跳转）。纯函数（除写 meta 外无副作用），可直接单测。"""
    norm_pages = ["".join(p.split()) for p in page_texts]
    start_page = 0
    for leaf in leaves:
        needle = "".join(leaf.text.split())[:prefix_chars]
        if not needle:
            continue
        for i in range(start_page, len(norm_pages)):
            if needle in norm_pages[i]:
                leaf.meta["page"] = i + 1
                start_page = i          # 后续块从本页起找（同页多块常见）
                break


class IngestPipeline:
    def __init__(self, session_factory, chunker: Chunker, embedder: Embedder,
                 store: VectorStore, files_dir: Path, keyword_index=None,
                 enricher=None, ocr_backend=None, embedder_resolver=None,
                 vlm_provider_resolver=None):
        self._sf = session_factory
        self._chunker = chunker
        self._embedder = embedder
        self._store = store
        self._files_dir = Path(files_dir)
        self._keyword_index = keyword_index
        self._enricher = enricher
        self._ocr = ocr_backend
        # M5-2 KB 级向量模型：resolver(kb_id) 返回该库绑定的 embedder；
        # 未提供（直接构造 pipeline 的测试/脚本）时退回构造参数里的单一 embedder。
        self._embedder_resolver = embedder_resolver
        # F VLM 深度识别：() -> provider dict（含密钥），services 按
        # cfg.vlm_parse.provider（缺省=当前活跃 provider）从 DB 解析。
        # 未提供时选择 vlm 模式的上传会判 failed 并附可读原因。
        self._vlm_provider_resolver = vlm_provider_resolver

    def ingest_file(self, kb_id: str, path: Path, original_name: str,
                    parse_mode: str = "auto") -> str:
        content_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        with self._sf() as s:
            dup = s.query(Document).filter_by(
                kb_id=kb_id, content_hash=content_hash).first()
            if dup:
                return dup.id
            doc = Document(id=str(uuid.uuid4()), kb_id=kb_id,
                           filename=original_name, content_hash=content_hash,
                           status="parsing", source_path=str(path),
                           parse_mode=parse_mode)
            s.add(doc)
            try:
                s.commit()
            except IntegrityError:
                # D4：并发摄取同一文件时，两个线程都通过了上面的查重（竞态
                # 窗口），第二个 commit 撞 uq_doc_kb_hash 唯一约束。这不是
                # 真正的失败——另一线程已经在插入相同内容的文档，这里回滚
                # 后在一个全新 session 里重查，返回那一行的 id 即可，不再
                # 跑一遍解析/摄取。
                s.rollback()
                with self._sf() as s2:
                    existing = s2.query(Document).filter_by(
                        kb_id=kb_id, content_hash=content_hash).first()
                    if existing is not None:
                        return existing.id
                raise
            doc_id = doc.id
        self._run(kb_id, doc_id, path, original_name, parse_mode)
        return doc_id

    def retry_document(self, doc_id: str) -> None:
        """对既有文档（通常 pending_ocr/failed）重跑解析，按文档落库的
        parse_mode 重走同一条路径。用 Document.source_path 找回原始文件，
        不依赖上传目录反查（上传文件名含 uuid 前缀无法直接推导）。"""
        with self._sf() as s:
            doc = s.get(Document, doc_id)
            if doc is None:
                return
            kb_id, path, name = doc.kb_id, doc.source_path, doc.filename
            parse_mode = doc.parse_mode or "auto"
        if not path or not Path(path).exists():
            self._set_status(doc_id, "failed", error="原始文件已丢失，无法重试")
            return
        self._set_status(doc_id, "parsing")
        self._run(kb_id, doc_id, Path(path), name, parse_mode)

    def _run(self, kb_id: str, doc_id: str, path: Path, name: str,
             parse_mode: str = "auto") -> None:
        try:
            status, ocr_confidence = self._process(kb_id, doc_id, path, name,
                                                   parse_mode)
            if status == "pending_review":
                # F：VLM 识别完成但未向量化——等人工校验确认（approve_document）
                self._set_status(doc_id, "pending_review")
            else:
                self._set_status(doc_id, "ready", ocr_confidence=ocr_confidence)
        except OCRUnavailable:
            # OCR 服务暂时不可达/超时：可重试，不是文档本身的问题
            self._set_status(doc_id, "pending_ocr")
        except Exception as e:  # noqa: BLE001 —— 批次隔离，失败落库
            self._set_status(doc_id, "failed", error=f"{type(e).__name__}: {e}")

    def _process(self, kb_id: str, doc_id: str, path: Path, name: str,
                 parse_mode: str = "auto"):
        suffix = Path(path).suffix.lower()

        # F VLM 深度识别：仅图片格式走此分支（PPT/PDF 等继续既有管道——
        # 逐页转图依赖重型组件，v1 不做，文档如实注明）。识别产物只落盘，
        # **不分块不向量化**，状态停 pending_review 等人工校验（防幻觉入库）。
        if parse_mode == "vlm" and suffix in _IMAGE_EXTS:
            markdown = self._vlm_markdown(path).replace("\x0c", "\n")
            if not markdown.strip():
                raise ValueError("VLM 识别结果为空")
            out_dir = self._files_dir / doc_id
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "content.md").write_text(markdown, encoding="utf-8")
            return ("pending_review", None)
        # 表格增强模式（parse_mode="ocr"）：文本层 PDF 也强制走 GLM-OCR——
        # pdfminer 不产表格结构（PDF 表格提出来是松散文本行），而 GLM-OCR
        # 输出 HTML <table>，能吃到表格原子分块+行线性化+跨页断表合并全套
        # 待遇。代价是丢失文本层页码定位（OCR 路径无逐页匹配），含表格的
        # PDF 值得这个交换。非 PDF/图片文件该模式等同 auto。
        force_ocr = parse_mode == "ocr" and (suffix == ".pdf"
                                             or suffix in _IMAGE_EXTS)
        needs_ocr = force_ocr or suffix in _IMAGE_EXTS or (
            suffix == ".pdf" and not pdf_has_text_layer(path))
        ocr_confidence = None
        ocr_layout = None
        odl_page_texts = None    # opendataloader 同源页文本（仅文本层 PDF 主路产出）
        if needs_ocr:
            if self._ocr is None:
                raise ValueError("扫描件/图片需要 OCR，当前未配置 OCR 后端")
            result = self._ocr.to_markdown(path)      # OCRUnavailable 向上抛，由 _run 捕获
            markdown = result.markdown
            ocr_confidence = result.confidence
            # GLM-OCR 的版式明细（bbox/label/表格结构，M6 表格版）：整份存档
            # 供后续 bbox 引用高亮等使用；表格语义本身已随 md_results 里的
            # Markdown 表格进入表格感知分块，不依赖这份明细。
            ocr_layout = getattr(result, "layout", None)
        else:
            markdown = None
            if suffix == ".pdf":
                # 文本层 PDF 主路：opendataloader（真实标题层级/阅读序/边框
                # 表格 + 同源页文本），不可用或失败返回 None 落回 markitdown
                # ——回退即升级前现状，见 pdf_odl 模块 docstring。
                from kbase.ingest import pdf_odl
                parsed = pdf_odl.parse_pdf(path)
                if parsed is not None:
                    markdown, odl_page_texts = parsed
            if markdown is None:
                from markitdown import MarkItDown
                markdown = MarkItDown(enable_plugins=False).convert(str(path)).text_content
        # \x0c（form feed）是 pdfminer/markitdown 的**页分隔符**，属于合法
        # 解析产物而非二进制垃圾——必须在下面的控制字符防线之前归一为换行，
        # 否则任何多页文本层 PDF 都会被误判"损坏"而摄取失败（M5-2 引用定位
        # 测试撞出的存量 bug：此前评测/压测语料全是 .md，该路径未被真实
        # 多页 PDF 走过）。
        markdown = markdown.replace("\x0c", "\n")
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
        if ocr_layout:
            (out_dir / "layout.json").write_text(
                json.dumps(ocr_layout, ensure_ascii=False), encoding="utf-8")

        # 多模态回答（图片）：提取文档内嵌插图落库——PDF 按页锚定、docx 按
        # 章节标题锚定（caption 级），回答引用命中时随 citations 附图。
        # 提图失败只损失附图能力，不阻塞摄取（与页码定位同一容错哲学）。
        if suffix == ".pdf" and not needs_ocr:
            try:
                from kbase.doc_images import extract_pdf_images
                extract_pdf_images(self._sf, doc_id, path, out_dir / "images")
            except Exception as e:  # noqa: BLE001
                logger.warning("文档 %s 内嵌图片提取失败（不影响摄取）: %s",
                               doc_id, e)
        elif suffix == ".docx":
            try:
                from kbase.doc_images import extract_docx_images
                extract_docx_images(self._sf, doc_id, path, out_dir / "images")
            except Exception as e:  # noqa: BLE001
                logger.warning("文档 %s docx 插图提取失败（不影响摄取）: %s",
                               doc_id, e)

        self._index_markdown(
            kb_id, doc_id, markdown, name,
            pdf_locate_path=(path if (suffix == ".pdf" and not needs_ocr) else None),
            page_texts=odl_page_texts)
        return ("ready", ocr_confidence)

    def _index_markdown(self, kb_id: str, doc_id: str, markdown: str, name: str,
                        pdf_locate_path=None, page_texts=None) -> None:
        """Markdown → 分块 → [页码定位] → [enrich] → 向量化 → chunk 行 →
        关键词索引。既有摄取与 F 的确认入库（approve_document）共用这一段，
        保证两条路径的索引语义完全一致。"""
        kb_config = self._load_kb_config(kb_id)
        chunker = self._chunker_for(kb_config)
        chunks = chunker.chunk(markdown, doc_name=name)
        leaves = [c for c in chunks if c.parent_id is not None]

        # 引用定位（M5-2）：文本层 PDF 逐页匹配叶子块页码。任何失败只损失
        # 定位能力，不阻塞摄取（页码是增强元数据，不是硬依赖）。
        # page_texts 优先（opendataloader 路：与 markdown 同一次解析产出，
        # 文本形态一致）；缺省回退 pdfminer 二次提取（markitdown 路现状）。
        if leaves and (page_texts or pdf_locate_path is not None):
            try:
                locate_chunk_pages(
                    page_texts or _pdf_page_texts(pdf_locate_path), leaves)
            except Exception:  # noqa: BLE001
                pass

        if leaves and self._enricher is not None and kb_config.get("enrich", {}).get("enabled"):
            leaves = self._enricher.enrich(name, markdown, leaves)

        if leaves:
            # 只嵌入叶子块；父块仅存 SQLite 供上下文组装。
            # 超长文本会被 embedding 模型静默截断，叶子块 512 字符远低于上限。
            # 嵌入文本组成（enrich 前缀/表格线性化）统一走 kbase/embed_text.py。
            embedder = (self._embedder_resolver(kb_id)
                        if self._embedder_resolver else self._embedder)
            vectors = embedder.embed(
                [embed_input(c.meta.get("enrich_context"), c.heading_path,
                             c.text, c.meta.get("layout"))
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
                            enrich_context=c.meta.get("enrich_context"),
                            page=c.meta.get("page"),
                            layout=(json.dumps(c.meta["layout"], ensure_ascii=False)
                                    if c.meta.get("layout") else None)))
            s.commit()

        if self._keyword_index and leaves:
            # 关键词索引文本组成统一走 kbase/embed_text.py（无 enrich 前缀；
            # 表格块用行线性化文本让单元格值可被 BM25 精确命中）。
            self._keyword_index.index(
                kb_id, [(c.id, doc_id,
                         keyword_input(c.heading_path, c.text, c.meta.get("layout")))
                        for c in leaves])

    def _vlm_markdown(self, path) -> str:
        """调满血视觉模型识别图片。provider 解析失败给可读错误（文档判
        failed 时用户能看懂该去配什么）。"""
        if self._vlm_provider_resolver is None:
            raise ValueError("VLM 深度识别未配置（缺 provider 解析器）")
        provider = self._vlm_provider_resolver()
        if provider is None:
            raise ValueError("VLM 深度识别的 provider 不存在，请在设置页配置视觉模型")
        from kbase import vlm_parse
        return vlm_parse.parse_image(path, provider)

    def _clear_doc_index(self, kb_id: str, doc_id: str) -> None:
        """清空该文档的全部索引痕迹（向量/关键词/chunk 行），供确认入库前
        幂等清场——重复确认/重新识别不会残留旧块。"""
        self._store.delete(kb_id, doc_id)
        if self._keyword_index is not None:
            self._keyword_index.delete_doc(doc_id)
        with self._sf() as s:
            s.query(Chunk).filter_by(doc_id=doc_id).delete()
            s.commit()

    def approve_document(self, doc_id: str, markdown: str | None = None) -> bool:
        """F 校验确认入库：管理员核对（可编辑）VLM 识别文本后调用——写回
        content.md → 清旧索引 → 分块向量化 → ready。返回 False=文档不存在；
        状态不是 pending_review 时抛 ValueError（路由转 409）。"""
        with self._sf() as s:
            doc = s.get(Document, doc_id)
            if doc is None:
                return False
            if doc.status != "pending_review":
                raise ValueError(f"仅待确认状态可执行入库，当前: {doc.status}")
            kb_id, name = doc.kb_id, doc.filename
        content_path = self._files_dir / doc_id / "content.md"
        if markdown is not None:
            content_path.parent.mkdir(parents=True, exist_ok=True)
            content_path.write_text(markdown, encoding="utf-8")
        if not content_path.exists():
            raise ValueError("识别结果文件缺失，请重试识别")
        text = content_path.read_text(encoding="utf-8")
        self._clear_doc_index(kb_id, doc_id)
        self._index_markdown(kb_id, doc_id, text, name)
        self._set_status(doc_id, "ready")
        return True

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
