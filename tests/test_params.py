"""结构化参数抽取与范围判定的单测（纯函数直击，无 IO）。

本文件钉住三条不变量：
1. 抽不出来就不抽——无单位、未知单位、覆盖率不足的列一律不收录；
2. 单位归一往返正确；
3. 区间重叠判定是"重叠"而非"包含"。
"""
from kbase.params import (
    extract_group_params,
    flatten_params,
    group_matches_range,
    is_range_condition,
    layout_param_bounds,
    normalize_unit,
    numeric_bounds,
    param_field_names,
    parse_cell,
)


# ---------------------------------------------------------------- parse_cell

def test_single_value_with_unit():
    pv = parse_cell("500W")
    assert (pv.lo, pv.hi, pv.unit) == (500.0, 500.0, "W")


def test_unit_normalized_to_base():
    pv = parse_cell("1.5kW")
    assert (pv.lo, pv.hi, pv.unit) == (1500.0, 1500.0, "W")


def test_chinese_unit():
    pv = parse_cell("500 瓦")
    assert (pv.lo, pv.hi, pv.unit) == (500.0, 500.0, "W")


def test_space_between_number_and_unit():
    assert parse_cell("1.5 kW").lo == 1500.0


def test_thousands_separator():
    assert parse_cell("1,200W").lo == 1200.0


def test_range_value():
    pv = parse_cell("400-600W")
    assert (pv.lo, pv.hi, pv.unit) == (400.0, 600.0, "W")


def test_range_with_chinese_dash():
    pv = parse_cell("400~600W")
    assert (pv.lo, pv.hi) == (400.0, 600.0)


def test_lower_bound_only():
    pv = parse_cell("≥300CFM")
    assert (pv.lo, pv.hi, pv.unit) == (300.0, None, "CFM")


def test_upper_bound_only():
    pv = parse_cell("<50dB")
    assert (pv.lo, pv.hi, pv.unit) == (None, 50.0, "dB")


def test_tolerance():
    pv = parse_cell("3.5±0.1mm")
    assert pv.unit == "m"
    assert abs(pv.lo - 0.0034) < 1e-9
    assert abs(pv.hi - 0.0036) < 1e-9


def test_bare_number_is_rejected():
    """无单位不收：同列混着 W 和 kW 时按裸数字比大小会无声出错。"""
    assert parse_cell("500") is None


def test_unknown_unit_is_rejected():
    assert parse_cell("500 furlong") is None


def test_non_numeric_is_rejected():
    assert parse_cell("不锈钢") is None
    assert parse_cell("见附录") is None
    assert parse_cell("") is None
    assert parse_cell(None) is None


def test_kelvin_not_in_table():
    """开氏需要偏移量而非乘系数，混进来会算错，故意不收。"""
    assert parse_cell("300K") is None


# ------------------------------------------------------------ normalize_unit

def test_normalize_roundtrip():
    assert normalize_unit(1.5, "kW") == (1500.0, "W")
    assert normalize_unit(3.5, "mm") == (0.0035, "m")
    assert normalize_unit(2.0, "t") == (2000.0, "kg")


def test_normalize_unknown_unit():
    assert normalize_unit(1.0, "zzz") is None


# ------------------------------------------------------ extract_group_params

def test_extract_basic_columns():
    header = ["型号", "功率", "风量"]
    rows = [["A1", "500W", "420CFM"],
            ["A2", "800W", "610CFM"],
            ["A3", "1.2kW", "900CFM"]]
    params = extract_group_params(header, rows)
    assert params["功率"] == {"min": 500.0, "max": 1200.0, "unit": "W"}
    assert params["风量"] == {"min": 420.0, "max": 900.0, "unit": "CFM"}
    assert "型号" not in params          # 非数值列不收录


def test_column_below_coverage_threshold_is_dropped():
    """某列一半单元格解析失败 → 整列不收录（半通的列比没有更危险）。"""
    header = ["功率"]
    rows = [["500W"], ["待定"], ["见附录"], ["800W"]]
    assert extract_group_params(header, rows) == {}


def test_column_at_coverage_threshold_is_kept():
    header = ["功率"]
    rows = [["500W"], ["600W"], ["700W"], ["待定"]]   # 3/4 = 75% ≥ 60%
    assert "功率" in extract_group_params(header, rows)


def test_empty_cells_do_not_count_against_coverage():
    """残缺行常见，空单元格不进分母，否则会误杀正常列。"""
    header = ["功率"]
    rows = [["500W"], [""], ["600W"], [""]]
    assert extract_group_params(header, rows)["功率"]["min"] == 500.0


def test_mixed_base_units_column_is_dropped():
    """同列混量纲说明解析串了或数据脏，宁可整列不收。"""
    header = ["参数"]
    rows = [["500W"], ["300CFM"], ["800W"]]
    assert extract_group_params(header, rows) == {}


def test_ragged_row_shorter_than_header():
    header = ["型号", "功率"]
    rows = [["A1", "500W"], ["A2"]]          # 第二行缺列
    assert extract_group_params(header, rows)["功率"]["min"] == 500.0


def test_empty_input():
    assert extract_group_params([], []) == {}
    assert extract_group_params(["功率"], []) == {}


def test_bound_only_column():
    header = ["风量"]
    rows = [["≥300CFM"], ["≥500CFM"]]
    params = extract_group_params(header, rows)
    assert params["风量"]["min"] == 300.0
    assert params["风量"]["max"] is None


# -------------------------------------------------------- 过滤侧 canonical

def test_is_range_condition_only_for_dict():
    """标量与列表必须仍走旧语义——这是向后兼容的第一道门。"""
    assert is_range_condition({"gte": 1}) is True
    assert is_range_condition({"approx": 500}) is True
    assert is_range_condition("专业服务") is False
    assert is_range_condition(["a", "b"]) is False
    assert is_range_condition({"其它键": 1}) is False


def test_numeric_bounds_gte_lte():
    assert numeric_bounds({"gte": 450, "lte": 550}) == (450.0, 550.0)
    assert numeric_bounds({"gte": 300}) == (300.0, None)
    assert numeric_bounds({"lte": 50}) == (None, 50.0)


def test_numeric_bounds_approx_expands():
    lo, hi = numeric_bounds({"approx": 500, "tol": 0.1})
    assert (lo, hi) == (450.0, 550.0)


def test_numeric_bounds_approx_default_tolerance():
    lo, hi = numeric_bounds({"approx": 100})
    assert (lo, hi) == (95.0, 105.0)


def test_numeric_bounds_invalid():
    assert numeric_bounds({}) is None
    assert numeric_bounds({"gte": "abc"}) is None
    assert numeric_bounds("not a dict") is None


def test_param_field_names():
    assert param_field_names("功率") == ("p_功率_min", "p_功率_max")


# ------------------------------------------------------ group_matches_range

def test_overlap_not_containment():
    """块区间 450~600、查 500 必须命中——用包含判定会把它判负。"""
    assert group_matches_range(450.0, 600.0, 500.0, 500.0) is True


def test_partial_overlap_matches():
    assert group_matches_range(400.0, 500.0, 480.0, 600.0) is True


def test_disjoint_below():
    assert group_matches_range(100.0, 200.0, 450.0, 550.0) is False


def test_disjoint_above():
    assert group_matches_range(800.0, 900.0, 450.0, 550.0) is False


def test_open_ended_query():
    assert group_matches_range(500.0, 500.0, 300.0, None) is True
    assert group_matches_range(500.0, 500.0, None, 400.0) is False


def test_missing_param_does_not_match():
    """与既有'meta 缺字段不匹配'语义一致。"""
    assert group_matches_range(None, None, 450.0, 550.0) is False


# ------------------------------------------------------------ payload 扁平化

def test_flatten_params():
    layout = {"kind": "table", "params": {"功率": {"min": 500.0, "max": 1200.0,
                                                   "unit": "W"}}}
    assert flatten_params(layout) == {"p_功率_min": 500.0, "p_功率_max": 1200.0}


def test_flatten_params_from_json_string():
    import json
    layout = json.dumps({"params": {"风量": {"min": 300.0, "max": None,
                                             "unit": "CFM"}}})
    assert flatten_params(layout) == {"p_风量_min": 300.0}


def test_flatten_params_tolerates_garbage():
    assert flatten_params(None) == {}
    assert flatten_params("not json") == {}
    assert flatten_params({"kind": "table"}) == {}
    assert flatten_params({"params": "not a dict"}) == {}


def test_layout_param_bounds():
    layout = {"params": {"功率": {"min": 500.0, "max": 1200.0, "unit": "W"}}}
    assert layout_param_bounds(layout, "功率") == (500.0, 1200.0)
    assert layout_param_bounds(layout, "风量") == (None, None)
    assert layout_param_bounds(None, "功率") == (None, None)
