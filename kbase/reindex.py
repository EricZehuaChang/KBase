"""重建索引：基于 SQLite 存量 chunk（不重新解析原始文件）回填 FTS 与向量。
用法：python -m kbase.reindex --kb <id> [--config config/kbase.yaml]"""
import argparse

from kbase.models import Chunk


def reindex_kb(session_factory, keyword_index, embedder, store, kb_id: str) -> int:
    with session_factory() as s:
        leaves = s.query(Chunk).filter_by(kb_id=kb_id, is_leaf=True).all()
    if not leaves:
        return 0
    keyword_index.delete_kb(kb_id)
    keyword_index.index(kb_id, [(c.id, c.doc_id, f"{c.heading_path}\n{c.text}")
                                for c in leaves])
    # 向量化文本组成与摄取管道一致：enrich_context（若有）作为前缀，见
    # ingest/pipeline.py 中相同的 lstrip 组合逻辑
    texts = [f"{(c.enrich_context or '')}\n{c.heading_path}\n{c.text}".lstrip()
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

    import kbase.plugins.embedders.bge_local      # noqa: F401
    import kbase.plugins.vectorstores.chroma_store  # noqa: F401

    embedder = registry.create("embedder", cfg.embedder.name, model=cfg.embedder.model)
    store = registry.create("vectorstore", cfg.vectorstore.name,
                            persist_dir=str(cfg.data_dir / "chroma"))

    n = reindex_kb(sf, kw, embedder, store, kb_id=args.kb)
    print(f"重建完成：kb={args.kb} 叶子块={n}")


if __name__ == "__main__":
    _main()
