from kbase.db import make_session_factory
from kbase.index.keyword import KeywordIndex
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import KnowledgeBase
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import ChromaStore
from kbase.reindex import reindex_kb

MD = "# 办法\n## 一章\n新兵办发〔2014〕76号相关内容。\n"


def test_reindex_backfills_fts(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / "c"))
    # 摄取时不带 keyword_index（模拟 M1 存量库）
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=200, chunk_overlap=0),
                              fake_embedder, store, tmp_path / "f")
    f = tmp_path / "a.md"
    f.write_text(MD, encoding="utf-8")
    pipeline.ingest_file("kb1", f, "a.md")

    kw = KeywordIndex(factory)
    assert kw.search("kb1", "76号", top_k=3) == []          # 存量库无 FTS
    n = reindex_kb(factory, kw, fake_embedder, store, kb_id="kb1")
    assert n > 0
    assert kw.search("kb1", "76号", top_k=3)                 # 回填后命中


# ------------------------------------------------- T04/G06：旧表格补算参数区间

TABLE_MD = """# 风扇选型手册

| 型号 | 功率 | 风量 |
| --- | --- | --- |
| A1 | 500W | 420CFM |
| A2 | 800W | 610CFM |
"""


def _legacy_table_kb(tmp_path, fake_embedder):
    """造一个"改造前"的库：表格块 layout 里只有 kind+linearized，没有 params
    （表格参数抽取是后加能力，旧库的 layout 就是这个形状）。"""
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / "c"))
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=200,
                                                        chunk_overlap=0),
                              fake_embedder, store, tmp_path / "f")
    f = tmp_path / "t.md"
    f.write_text(TABLE_MD, encoding="utf-8")
    pipeline.ingest_file("kb1", f, "t.md")

    # 抹掉 params，还原旧库形状（同时抹掉向量 payload 里的扁平字段——
    # 旧库的向量库本来就没有这些字段）
    import json as _json
    from kbase.models import Chunk
    with factory() as s:
        table = (s.query(Chunk).filter_by(kb_id="kb1", is_leaf=True)
                 .filter(Chunk.layout.isnot(None)).first())
        assert table is not None, "应有表格块"
        layout = _json.loads(table.layout)
        assert layout.get("kind") == "table"
        layout.pop("params", None)
        table.layout = _json.dumps(layout, ensure_ascii=False)
        s.commit()
        table_id = table.id
    store.delete_collection("kb1")          # 清掉带 params 的老向量
    return factory, store, table_id


def test_reindex_backfills_table_params_and_persists(tmp_path, fake_embedder):
    """缺 params 的旧表格块：reindex 补算 → DB layout 有 params、
    向量 payload 有扁平字段、范围过滤能命中。"""
    import json as _json

    from kbase.models import Chunk
    from kbase.params import flatten_params
    from kbase.rag.retriever import chunk_meta_matches

    factory, store, table_id = _legacy_table_kb(tmp_path, fake_embedder)
    stats: dict = {}
    n = reindex_kb(factory, None, fake_embedder, store, kb_id="kb1", stats=stats)
    assert n > 0
    assert stats["table_params_backfilled"] == 1

    # 1) DB layout 落回了 params（不落库下次重建还是缺）
    with factory() as s:
        layout = _json.loads(s.get(Chunk, table_id).layout)
    assert layout["params"]["功率"] == {"min": 500.0, "max": 800.0, "unit": "W"}
    assert layout["params"]["风量"] == {"min": 420.0, "max": 610.0, "unit": "CFM"}
    # 线性化文本没被改动（检索质量支柱，见 test_chunker_table_linearization_unchanged）
    assert "A1" in layout["linearized"]

    # 2) 向量 payload 带上了扁平字段
    payload = flatten_params(_json.dumps(layout))
    assert payload["p_功率_min"] == 500.0 and payload["p_功率_max"] == 800.0

    # 3) 范围条件对该块命中（关键词路后过滤的判定入口）
    with factory() as s:
        row = s.get(Chunk, table_id)
        assert chunk_meta_matches(row.meta, {"功率": {"gte": 450, "lte": 550}},
                                  row.layout) is True
        assert chunk_meta_matches(row.meta, {"功率": {"gte": 5000, "lte": 6000}},
                                  row.layout) is False


def test_reindex_backfill_disabled_keeps_old_behaviour(tmp_path, fake_embedder):
    """--no-backfill-params 关闭时行为与改造前一致：layout 仍缺 params。"""
    import json as _json

    from kbase.models import Chunk

    factory, store, table_id = _legacy_table_kb(tmp_path, fake_embedder)
    stats: dict = {}
    reindex_kb(factory, None, fake_embedder, store, kb_id="kb1",
               backfill_params=False, stats=stats)
    assert "table_params_backfilled" not in stats
    with factory() as s:
        layout = _json.loads(s.get(Chunk, table_id).layout)
    assert "params" not in layout


def test_backfill_keeps_existing_params(tmp_path, fake_embedder):
    """已有 params 的块（含运营人工校正过的）不被重算覆盖。"""
    import json as _json

    from kbase.models import Chunk
    from kbase.reindex import backfill_table_params

    factory, store, table_id = _legacy_table_kb(tmp_path, fake_embedder)
    with factory() as s:
        row = s.get(Chunk, table_id)
        layout = _json.loads(row.layout)
        layout["params"] = {"功率": {"min": 1.0, "max": 2.0, "unit": "W"}}
        row.layout = _json.dumps(layout, ensure_ascii=False)
        s.commit()

    assert backfill_table_params(factory, "kb1") == 0
    with factory() as s:
        layout = _json.loads(s.get(Chunk, table_id).layout)
    assert layout["params"]["功率"]["min"] == 1.0      # 人工值原样保留


def test_backfill_skips_non_table_and_broken_layout(tmp_path, fake_embedder):
    """非表格块 / 坏 JSON layout / 已不是合法表格的块都不动，也不该抛。

    夹具里那张缺 params 的真表格先被正常补算（返回 1），下面三个畸形块
    一个都不该被计入——断言"只有真表格被补算"。
    """
    import json as _json

    from kbase.models import Chunk
    from kbase.reindex import backfill_table_params

    factory, store, _ = _legacy_table_kb(tmp_path, fake_embedder)
    with factory() as s:
        s.add(Chunk(id="c-broken", doc_id="d1", kb_id="kb1", is_leaf=True,
                    heading_path="h", text="| a | b |\n| --- | --- |\n| 1 | 2 |",
                    layout="{坏 JSON"))
        s.add(Chunk(id="c-plain", doc_id="d1", kb_id="kb1", is_leaf=True,
                    heading_path="h", text="普通段落", layout='{"kind":"text"}'))
        s.add(Chunk(id="c-notable", doc_id="d1", kb_id="kb1", is_leaf=True,
                    heading_path="h", text="已经不是表格了",
                    layout='{"kind":"table","linearized":"旧"}'))
        s.commit()
    assert backfill_table_params(factory, "kb1") == 1     # 只有夹具里那张真表格
    with factory() as s:
        assert s.get(Chunk, "c-broken").layout == "{坏 JSON"      # 坏 JSON 原样不动
        assert "params" not in _json.loads(s.get(Chunk, "c-notable").layout)
        assert "params" not in _json.loads(s.get(Chunk, "c-plain").layout)
