"""重建索引：基于 SQLite 存量 chunk（不重新解析原始文件）回填 FTS 与向量。
用法：python -m kbase.reindex --kb <id> [--config config/kbase.yaml]
                            [--no-backfill-params]

T04/G06：重建时顺带为**旧表格块补算参数区间**（layout.params）。表格参数
抽取（kbase/params.py）是后加的能力，此前摄取的表格块 layout 里根本没有
params 键，`flatten_params` 自然摊不出扁平字段——旧库的范围过滤对这些历史
表格永远不命中，且每次重建都白跑一遍。补算结果**必须落回 DB**（否则下次
重建还是缺），见 `backfill_table_params`。
"""
import argparse
import json

from kbase.models import Chunk


def backfill_table_params(session_factory, kb_id: str) -> int:
    """为缺 params 的表格叶子块补算参数区间并落库，返回补算块数。

    只处理 `layout.kind == "table"` 且 `params` 缺失/为空的块；已有 params
    的块一律不动（可能被运营手工校过，重算会覆盖人工结果）。补算与摄取、
    运营编辑走同一个 `extract_group_params`——三处必须同源，否则同一张表在
    不同路径下得到不同区间（见 kbase/params.py 的模块说明）。
    """
    from kbase.params import extract_group_params
    from kbase.plugins.chunkers.structure import parse_table

    fixed = 0
    with session_factory() as s:
        rows = s.query(Chunk).filter_by(kb_id=kb_id, is_leaf=True).all()
        for c in rows:
            if not c.layout:
                continue
            try:
                layout = json.loads(c.layout)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(layout, dict) or layout.get("kind") != "table":
                continue
            if layout.get("params"):
                continue                    # 已有参数（含人工校正过）不重算
            parsed = parse_table(c.text)
            if parsed is None:
                continue                    # 文本已不是合法表格：不该有 params
            params = extract_group_params(*parsed)
            if not params:
                continue                    # 抽不出数值列 = 正常结果，不写键
            layout["params"] = params
            c.layout = json.dumps(layout, ensure_ascii=False)
            fixed += 1
        if fixed:
            s.commit()
    return fixed


def reindex_kb(session_factory, keyword_index, embedder, store, kb_id: str,
               *, backfill_params: bool = True,
               stats: dict | None = None) -> int:
    """重建 kb 的 FTS/向量索引，返回回填的叶子块数。

    stats：可选输出参数，回填 `{"table_params_backfilled": n}`——补算数对
    调用方（CLI 报数、运维确认"旧库这次真吃到范围过滤"）有用，但返回值语义
    必须保持"叶子块数"不变（既有调用方按 int 消费）。
    """
    if backfill_params:
        fixed = backfill_table_params(session_factory, kb_id)
        if stats is not None:
            stats["table_params_backfilled"] = fixed
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
    # T04/G06：默认补算旧表格块的参数区间（--no-backfill-params 关闭，
    # 行为退回本次改造之前）
    parser.add_argument("--backfill-params", dest="backfill_params",
                        action="store_true", default=True,
                        help="为缺 params 的历史表格块补算参数区间（默认开）")
    parser.add_argument("--no-backfill-params", dest="backfill_params",
                        action="store_false",
                        help="不补算历史表格参数（只重建索引）")
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

    stats: dict = {}
    n = reindex_kb(sf, kw, embedder, store, kb_id=args.kb,
                   backfill_params=args.backfill_params, stats=stats)
    # 补算数单独报出来：它是"旧库这次真吃到范围过滤红利"的可观测信号
    note = ("" if args.backfill_params
            else "（--no-backfill-params：未补算历史表格参数）")
    print(f"重建完成：kb={args.kb} 叶子块={n} "
          f"补算表格参数={stats.get('table_params_backfilled', 0)} 块{note}")


if __name__ == "__main__":
    _main()
