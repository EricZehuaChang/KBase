"""范围过滤的三路一致性契约 + 向后兼容回归。

本文件是这次改造最重要的一道闸门，钉两件事：

1. **向后兼容**：纯值/列表形态的 filters 行为字节级不变。ztenith 方案卡与
   KMBP 名单库在用它做隔离，任何漂移都是线上事故。
2. **三路同结果**：Qdrant 稠密路、Chroma 稠密路、BM25 关键词路后过滤，
   对同一份数据 + 同一组 filters 必须返回同一个 chunk_id 集合。三者语义
   漂移的 bug 在演示环境测不出来，只在客户现场炸。
"""
import json

from kbase.params import (
    is_range_condition,
    numeric_bounds,
    param_field_names,
)
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import _chroma_matches
from kbase.rag.retriever import chunk_meta_matches

# ---------------------------------------------------------------------------
# 三个块的固定夹具：功率区间分别为 [500,700] / [800,1200] / 无参数
# ---------------------------------------------------------------------------
BLOCKS = {
    "c1": {"params": {"功率": {"min": 500.0, "max": 700.0, "unit": "W"}}},
    "c2": {"params": {"功率": {"min": 800.0, "max": 1200.0, "unit": "W"}}},
    "c3": {"kind": "table"},                      # 表格块但没抽出参数
}
DOC_META = {"c1": {"industry": "零售"}, "c2": {"industry": "制造"}, "c3": None}


def _keyword_path(filters: dict) -> set[str]:
    """关键词路：chunk_meta_matches(meta_json, filters, layout_json)。"""
    out = set()
    for cid, layout in BLOCKS.items():
        meta = DOC_META[cid]
        if chunk_meta_matches(json.dumps(meta) if meta else None, filters,
                              json.dumps(layout)):
            out.add(cid)
    return out


def _chroma_path(filters: dict) -> set[str]:
    """Chroma 稠密路：payload 是扁平化后的字段。"""
    out = set()
    for cid, layout in BLOCKS.items():
        payload = dict(DOC_META[cid] or {})
        for name, spec in (layout.get("params") or {}).items():
            fmin, fmax = param_field_names(name)
            if spec.get("min") is not None:
                payload[fmin] = spec["min"]
            if spec.get("max") is not None:
                payload[fmax] = spec["max"]
        if _chroma_matches(payload, filters):
            out.add(cid)
    return out


def _qdrant_path(filters: dict) -> set[str]:
    """Qdrant 稠密路：复算适配器构造的 must 条件语义。

    不起真实 Qdrant（部署是 lite 档，CI 也没有该服务），而是按适配器里
    完全相同的分派逻辑判定——两者共用 kbase/params.py 的同一组函数，
    这里验证的是"同一组函数被同样地使用"。
    """
    out = set()
    for cid, layout in BLOCKS.items():
        payload = dict(DOC_META[cid] or {})
        for name, spec in (layout.get("params") or {}).items():
            fmin, fmax = param_field_names(name)
            if spec.get("min") is not None:
                payload[fmin] = spec["min"]
            if spec.get("max") is not None:
                payload[fmax] = spec["max"]
        ok = True
        for k, v in filters.items():
            if is_range_condition(v):
                bounds = numeric_bounds(v)
                if bounds is None:
                    continue
                lo, hi = bounds
                fmin, fmax = param_field_names(k)
                # Qdrant must：p_x_max >= lo 且 p_x_min <= hi（缺字段即不匹配）
                if lo is not None and (payload.get(fmax) is None
                                       or payload[fmax] < lo):
                    ok = False
                    break
                if hi is not None and (payload.get(fmin) is None
                                       or payload[fmin] > hi):
                    ok = False
                    break
                continue
            wanted = v if isinstance(v, list) else [v]
            stored = payload.get(k)
            if stored is None or str(stored) not in {str(w) for w in wanted}:
                ok = False
                break
        if ok:
            out.add(cid)
    return out


ALL_PATHS = (_keyword_path, _chroma_path, _qdrant_path)


def _agree(filters: dict) -> set[str]:
    """跑三条路，断言结果集完全一致，返回该集合。"""
    results = [p(filters) for p in ALL_PATHS]
    assert results[0] == results[1] == results[2], (
        f"三路结果不一致 filters={filters} "
        f"keyword={results[0]} chroma={results[1]} qdrant={results[2]}")
    return results[0]


# ---------------------------------------------------------- 三路一致性契约

def test_three_paths_agree_on_range_hit():
    assert _agree({"功率": {"gte": 450, "lte": 550}}) == {"c1"}


def test_three_paths_agree_on_range_miss():
    assert _agree({"功率": {"gte": 2000, "lte": 3000}}) == set()


def test_three_paths_agree_on_overlap_semantics():
    """查 600~900 与 c1[500,700] 和 c2[800,1200] 都有交集 → 两块都召回。"""
    assert _agree({"功率": {"gte": 600, "lte": 900}}) == {"c1", "c2"}


def test_three_paths_agree_on_open_upper_bound():
    assert _agree({"功率": {"gte": 750}}) == {"c2"}


def test_three_paths_agree_on_open_lower_bound():
    assert _agree({"功率": {"lte": 600}}) == {"c1"}


def test_three_paths_agree_on_approx():
    assert _agree({"功率": {"approx": 1000, "tol": 0.1}}) == {"c2"}


def test_three_paths_agree_when_param_absent():
    """c3 没有该参数 → 三路都不匹配（与'meta 缺字段不匹配'语义一致）。"""
    hit = _agree({"功率": {"gte": 0, "lte": 99999}})
    assert "c3" not in hit


def test_three_paths_agree_on_plain_equality():
    """纯值形态：三路仍须一致，且行为与改造前相同。"""
    assert _agree({"industry": "零售"}) == {"c1"}


def test_three_paths_agree_on_list_membership():
    assert _agree({"industry": ["零售", "制造"]}) == {"c1", "c2"}


def test_three_paths_agree_on_mixed_conditions():
    """范围条件与等值条件 AND 组合。"""
    assert _agree({"industry": "零售", "功率": {"gte": 450, "lte": 550}}) == {"c1"}
    assert _agree({"industry": "制造", "功率": {"gte": 450, "lte": 550}}) == set()


# ------------------------------------------------------------ 向后兼容回归

def test_plain_filters_behaviour_unchanged():
    """旧签名（两参）必须照常工作——存量调用方一个字都没改。"""
    meta = json.dumps({"industry": "零售", "data_entities": ["订单", "库存"]})
    assert chunk_meta_matches(meta, {"industry": "零售"}) is True
    assert chunk_meta_matches(meta, {"industry": "制造"}) is False
    assert chunk_meta_matches(meta, {"data_entities": "订单"}) is True
    assert chunk_meta_matches(meta, {"data_entities": ["库存", "物料"]}) is True
    assert chunk_meta_matches(None, {"industry": "零售"}) is False
    assert chunk_meta_matches("坏 JSON", {"industry": "零售"}) is False


def test_plain_filters_ignore_layout():
    """有 layout 也不能影响纯值判定。"""
    meta = json.dumps({"industry": "零售"})
    layout = json.dumps({"params": {"功率": {"min": 1.0, "max": 2.0, "unit": "W"}}})
    assert chunk_meta_matches(meta, {"industry": "零售"}, layout) is True
    assert chunk_meta_matches(meta, {"industry": "制造"}, layout) is False


def test_range_only_filter_does_not_require_doc_meta():
    """纯范围条件时 meta 为 NULL 不该导致不匹配——参数在 layout 里。

    这是刻意与"纯值条件下 meta 为 NULL 即不匹配"区分开的：两类条件读的是
    不同的数据源，不能让文档级 meta 的缺失误杀块级参数命中。
    """
    layout = json.dumps({"params": {"功率": {"min": 500.0, "max": 700.0,
                                             "unit": "W"}}})
    assert chunk_meta_matches(None, {"功率": {"gte": 450, "lte": 550}},
                              layout) is True


# --------------------------------------------------- 摄取端到端（分块→参数）

def test_chunker_attaches_params_to_table_block():
    md = """# 风扇选型

| 型号 | 功率 | 风量 |
| --- | --- | --- |
| A1 | 500W | 420CFM |
| A2 | 800W | 610CFM |
"""
    leaves = [c for c in StructureChunker(chunk_size=512).chunk(md, "选型手册")
              if c.parent_id is not None]
    tables = [c for c in leaves if c.meta.get("layout", {}).get("kind") == "table"]
    assert tables, "表格块应被识别"
    params = tables[0].meta["layout"]["params"]
    assert params["功率"] == {"min": 500.0, "max": 800.0, "unit": "W"}
    assert params["风量"] == {"min": 420.0, "max": 610.0, "unit": "CFM"}


def test_chunker_omits_params_for_non_numeric_table():
    md = """# 材质对照

| 型号 | 材质 |
| --- | --- |
| A1 | 不锈钢 |
| A2 | 铝合金 |
"""
    leaves = [c for c in StructureChunker(chunk_size=512).chunk(md, "手册")
              if c.parent_id is not None]
    tables = [c for c in leaves if c.meta.get("layout", {}).get("kind") == "table"]
    assert tables
    # 抽不出数值列 → 不写 params 键，行为与改造前一致
    assert "params" not in tables[0].meta["layout"]
    assert tables[0].meta["layout"]["linearized"]      # 线性化仍在


def test_chunker_table_linearization_unchanged():
    """行线性化是既有检索质量的支柱，改造不得动它。"""
    md = """# 差旅标准

| 城市级别 | 住宿上限 |
| --- | --- |
| 二线城市 | 每晚350元 |
"""
    leaves = [c for c in StructureChunker(chunk_size=512).chunk(md, "制度")
              if c.parent_id is not None]
    lin = leaves[0].meta["layout"]["linearized"]
    assert "城市级别=二线城市；住宿上限=每晚350元。" in lin
