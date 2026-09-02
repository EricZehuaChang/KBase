"""重建索引：基于 SQLite 存量 chunk（不重新解析原始文件）回填 FTS 与向量。
用法：python -m kbase.reindex --kb <id> [--config config/kbase.yaml]"""
import argparse

from kbase.models import Chunk


def reindex_kb(session_factory, keyword_index, embedder, store, kb_id: str) -> int:
    with session_factory() as s:
        leaves = s.query(Chunk).filter_by(kb_id=kb_id, is_leaf=True).all()
    # M6-1 停用块（enabled=False）不得随重建复活——停用语义=从索引摘除但
    # 行保留，重建只回填启用中的叶子。
    from kbase.chunk_admin import is_enabled
    leaves = [c for c in leaves if is_enabled(c)]
    if not leaves:
        return 0
    from kbase.embed_text import embed_input, keyword_input
    if keyword_index is not None:      # hybrid 关闭的部署没有关键词索引
        keyword_index.delete_kb(kb_id)
        keyword_index.index(kb_id, [(c.id, c.doc_id,
                                     keyword_input(c.heading_path, c.text, c.layout))
                                    for c in leaves])
    # 嵌入/关键词文本组成统一走 kbase/embed_text.py（与摄取管道同源）
    texts = [embed_input(c.enrich_context, c.heading_path, c.text, c.layout)
             for c in leaves]
    vectors = embedder.embed(texts)
    # payload 必须与摄取管道（ingest/pipeline.py）逐字段一致，否则重建后
    # 稠密路过滤静默失效——Qdrant 的 must 条件在缺字段时直接过滤掉全部候选。
    # 修既有缺陷：此处原本没铺文档级 front matter（Chunk.meta），重建后
    # 方案卡类 filters 在稠密路会失灵（关键词路读库所以还能工作，症状是
    # 两路结果不一致，很难排查）。
    import json as _json

    from kbase.params import flatten_params

    def _payload(c):
        out = {"doc_id": c.doc_id, "parent_id": c.parent_id}
        if c.meta:
            try:
                doc_meta = _json.loads(c.meta)
                if isinstance(doc_meta, dict):
                    out.update(doc_meta)
            except (ValueError, TypeError):
                pass
        out.update(flatten_params(c.layout))
        return out

    store.upsert(kb_id, ids=[c.id for c in leaves], vectors=vectors,
                 metas=[_payload(c) for c in leaves])
    return len(leaves)


def _main() -> None:
    parser = argparse.ArgumentParser(description="重建指定知识库的 FTS/向量索引")
    parser.add_argument("--kb", required=True, help="知识库 id")
    parser.add_argument("--config", default="config/kbase.yaml", help="配置文件路径")
    args = parser.parse_args()

    from kbase.config import load_config, resolve_db_url
    from kbase.db import make_session_factory
    from kbase.index.factory import make_keyword_index
    from kbase.plugins.registry import registry

    cfg = load_config(args.config)
    sf = make_session_factory(resolve_db_url(cfg))
    with sf() as _s:
        dialect = _s.get_bind().dialect.name
    kw = make_keyword_index(sf, dialect=dialect)

    import kbase.plugins.vectorstores.chroma_store  # noqa: F401

    # M5-2：重建必须用该 KB 绑定的向量模型（KB.config JSON 的 embedder 键），
    # 不能一律用默认模型——否则重建后的向量与查询向量空间不一致，检索报废。
    from kbase.plugins.embedders.factory import EmbedderPool, kb_embedder_id
    embedder = EmbedderPool(cfg).get(kb_embedder_id(sf, args.kb))
    store = registry.create("vectorstore", cfg.vectorstore.name,
                            persist_dir=str(cfg.data_dir / "chroma"))

    n = reindex_kb(sf, kw, embedder, store, kb_id=args.kb)
    print(f"重建完成：kb={args.kb} 叶子块={n}")


if __name__ == "__main__":
    _main()
