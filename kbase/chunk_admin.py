"""Chunk 运营管理（M6-1）：分块列表、启停、文本编辑与索引同步。

设计不变量：**检索可见性 = 索引成员资格**。停用一个叶子块 = 把它从向量库
与关键词索引中摘除（行保留，可恢复）；启用/编辑 = 重嵌入+重索引。检索层
（retriever）不需要感知 enabled 字段——索引里没有的块天然不可召回；
_assemble 里另有一道 enabled 防御兜底（索引清理万一失手也不外漏停用块）。

enabled 的 NULL 语义：老库补列后存量行为 NULL，一律按"启用"解释
（is_enabled 帮助函数统一判定，禁止散落 `== True` 判断）。

编辑范围：叶子块改文本 → 重嵌入（该 KB 绑定的向量模型）+ 关键词重索引；
父块改文本 → 仅落库（父块不进任何索引，只作为 small-to-big 的上下文，
改动在下次被引用时生效）。
"""
import json

from kbase.embed_text import embed_input, keyword_input
from kbase.models import Chunk, Document
from kbase.plugins.chunkers.structure import linearize_table, parse_table


def _refresh_table_layout(c: Chunk) -> None:
    """编辑表格块文本后重算线性化（M6 表格版）：新文本仍是合法表格 → 更新
    linearized；不再是表格 → 清掉 layout（退化为普通文本块，检索用原文）。
    非表格块不动。"""
    if not c.layout:
        return
    try:
        layout = json.loads(c.layout)
    except (json.JSONDecodeError, TypeError):
        return
    if layout.get("kind") != "table":
        return
    parsed = parse_table(c.text)
    if parsed is None:
        c.layout = None
        return
    layout["linearized"] = linearize_table(*parsed)
    c.layout = json.dumps(layout, ensure_ascii=False)


def is_enabled(chunk: Chunk) -> bool:
    """NULL（老库存量行）与 True 都算启用；只有显式 False 是停用。"""
    return chunk.enabled is not False


def _chunk_out(c: Chunk) -> dict:
    return {"id": c.id, "doc_id": c.doc_id, "heading_path": c.heading_path,
            "text": c.text, "is_leaf": c.is_leaf, "page": c.page,
            "enabled": is_enabled(c), "chars": len(c.text),
            "layout": json.loads(c.layout) if c.layout else None}


def list_chunks(sf, doc_id: str, offset: int = 0, limit: int = 50,
                q: str | None = None) -> dict | None:
    """按文档分页列出块（叶子在前、按主键稳定排序）。q 为可选的文本包含
    过滤（运营定位坏块用）。文档不存在返回 None（路由转 404）。"""
    with sf() as s:
        if s.get(Document, doc_id) is None:
            return None
        query = s.query(Chunk).filter_by(doc_id=doc_id)
        if q:
            query = query.filter(Chunk.text.contains(q))
        total = query.count()
        rows = (query.order_by(Chunk.is_leaf.desc(), Chunk.id)
                .offset(offset).limit(limit).all())
        return {"items": [_chunk_out(c) for c in rows], "total": total}


def update_chunk(sf, store, keyword_index, embedder_for_kb, chunk_id: str, *,
                 enabled: bool | None = None, text: str | None = None) -> dict | None:
    """启停/编辑一个块并同步索引。返回更新后的块视图；不存在返回 None。

    顺序约定：先索引后落库会在索引失败时留下"DB 已改索引没改"的脏态，
    这里反过来——**先做索引侧操作，成功后才提交 DB**，任何一步抛异常时
    DB 保持原状，重试语义干净（索引侧操作均幂等：delete_ids/upsert）。"""
    with sf() as s:
        c = s.get(Chunk, chunk_id)
        if c is None:
            return None
        kb_id = c.kb_id
        # 计算目标状态（在 session 内先改内存对象，索引成功后再 commit）
        if text is not None:
            c.text = text
            _refresh_table_layout(c)     # 表格块编辑后重算线性化（M6 表格版）
        if enabled is not None:
            c.enabled = enabled
        target_enabled = is_enabled(c)

        if c.is_leaf:
            if not target_enabled:
                # 停用：摘出两路索引成员
                store.delete_ids(kb_id, [c.id])
                if keyword_index is not None:
                    keyword_index.delete_ids([c.id])
            else:
                # 启用/编辑：重嵌入 + 重索引（FTS5 版 index() 是纯 INSERT，
                # 必须先 delete_ids 防重复行；PG 版 upsert 幂等，先删无害）
                embedder = embedder_for_kb(kb_id)
                vectors = embedder.embed([embed_input(
                    c.enrich_context, c.heading_path, c.text, c.layout)])
                store.upsert(collection=kb_id, ids=[c.id], vectors=vectors,
                             metas=[{"doc_id": c.doc_id, "parent_id": c.parent_id}])
                if keyword_index is not None:
                    keyword_index.delete_ids([c.id])
                    keyword_index.index(kb_id, [(c.id, c.doc_id, keyword_input(
                        c.heading_path, c.text, c.layout))])
        # 父块：不进索引，只落库（作为上下文在下次组装时生效）

        s.commit()
        s.refresh(c)
        return _chunk_out(c)
