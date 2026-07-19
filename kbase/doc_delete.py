"""文档删除级联（单一事实源）：路由删除端点与连接器同步引擎（变更重摄/
prune）共用。级联顺序是硬约束：向量 → 全文索引 → chunk/document_images 行
→ document 行 → files 目录（document_images 无 FK 级联，漏删会积累孤儿
死行——见 710d78a 修复）。"""
import shutil
from pathlib import Path

from kbase.models import Chunk, Document, DocumentImage


def delete_document_cascade(sf, store, keyword_index, data_dir: Path,
                            kb_id: str, doc_id: str) -> bool:
    """按级联顺序删除一份文档的全部痕迹。返回 False=文档不存在或不属于
    该库（幂等：重复删除/并发删除不报错）。"""
    with sf() as s:
        doc = s.get(Document, doc_id)
        if doc is None or doc.kb_id != kb_id:
            return False
    store.delete(kb_id, doc_id)
    if keyword_index is not None:
        keyword_index.delete_doc(doc_id)
    with sf() as s:
        s.query(Chunk).filter_by(doc_id=doc_id).delete()
        s.query(DocumentImage).filter_by(doc_id=doc_id).delete()
        doc = s.get(Document, doc_id)
        if doc is not None:
            s.delete(doc)
        s.commit()
    shutil.rmtree(Path(data_dir) / "files" / doc_id, ignore_errors=True)
    return True
