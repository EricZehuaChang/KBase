"""重建索引：基于 SQLite 存量 chunk（不重新解析原始文件）回填 FTS 与向量。
用法：python -m kbase.reindex --kb <id> [--config config/kbase.yaml]"""
import argparse

from kbase.models import Chunk


def reindex_kb(session_factory, keyword_index, embedder, store, kb_id: str) -> int:
    with session_factory() as s:
        leaves = s.query(Chunk).filter_by(kb_id=kb_id, is_leaf=True).all()
    if not leaves:
        return 0
    from kbase.embed_text import embed_input, keyword_input
    keyword_index.delete_kb(kb_id)
    keyword_index.index(kb_id, [(c.id, c.doc_id,
                                 keyword_input(c.heading_path, c.text, c.layout))
                                for c in leaves])
    # 嵌入/关键词文本组成统一走 kbase/embed_text.py（与摄取管道同源）
    texts = [embed_input(c.enrich_context, c.heading_path, c.text, c.layout)
             for c in leaves]
    vectors = embedder.embed(texts)
    store.upsert(kb_id, ids=[c.id for c in leaves], vectors=vectors,
                 metas=[{"doc_id": c.doc_id, "parent_id": c.parent_id}
                        for c in leaves])
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
