"""结构化参数抽取与范围过滤的**唯一权威**。

背景：filters 原本只做字符串等值/集合匹配，表达不了「功率 between 450 and 550」，
参数选型类问答（"500 瓦风扇风量如何选"）只能靠 LLM 对着整张表猜。本模块补上数值维度。

**为什么所有判定都收敛到这里**：范围过滤要在三条路上生效——Qdrant 稠密路、Chroma 稠密路、
BM25 关键词路后过滤。三处语义一旦漂移，同一个查询在 lite 与 standard 档上会给出不同答案，
而这种 bug 在演示环境测不出来、只在客户现场炸。所以三处**不是各自实现再靠注释同步**，
而是全部调用本模块的 `numeric_bounds` / `param_field_names` / `group_matches_range`，
语义由构造保证一致。（同类教训见 embed_text.py 顶部注释。）

**粒度选择**：参数按**行组**（表格块）存区间，不按行存。行级会让 500 行选型表变成
500 个 chunk，推翻既有十万文件体量标定；行组本就按 chunk_size 切分、天然能读进上下文，
过滤把候选从整本手册收窄到含目标值的那一组，组内由 LLM 挑行即可。

**不猜原则**：单元格解析不出确定数值与单位时不抽取，该列保持纯文本检索路径。
猜错的单位换算会产出看不出来的错答——比抽不到更糟。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 一列中能成功解析的单元格占比低于此阈值，整列不收录。
# 半通的列比没有更危险：过滤会静默漏掉解析失败的那些行。
_COLUMN_COVERAGE_MIN = 0.6

# 量纲换算到基准单位。放在模块常量而非硬编码进解析逻辑，便于按客户行业扩展。
# key 为小写单位符号，value 为 (乘到基准单位的系数, 基准单位)。
_UNIT_TABLE: dict[str, tuple[float, str]] = {
    # 功率
    "w": (1.0, "W"), "kw": (1000.0, "W"), "mw": (0.001, "W"), "瓦": (1.0, "W"),
    "千瓦": (1000.0, "W"),
    # 长度
    "m": (1.0, "m"), "mm": (0.001, "m"), "cm": (0.01, "m"), "km": (1000.0, "m"),
    "米": (1.0, "m"), "毫米": (0.001, "m"), "厘米": (0.01, "m"),
    # 质量
    "kg": (1.0, "kg"), "g": (0.001, "kg"), "t": (1000.0, "kg"),
    "千克": (1.0, "kg"), "克": (0.001, "kg"), "吨": (1000.0, "kg"),
    # 风量
    "cfm": (1.0, "CFM"), "m3/h": (0.5886, "CFM"), "m³/h": (0.5886, "CFM"),
    # 电压 / 电流
    "v": (1.0, "V"), "kv": (1000.0, "V"), "mv": (0.001, "V"), "伏": (1.0, "V"),
    "a": (1.0, "A"), "ma": (0.001, "A"), "安": (1.0, "A"),
    # 频率
    "hz": (1.0, "Hz"), "khz": (1000.0, "Hz"), "mhz": (1000000.0, "Hz"),
    "赫兹": (1.0, "Hz"),
    # 压力
    "pa": (1.0, "Pa"), "kpa": (1000.0, "Pa"), "mpa": (1000000.0, "Pa"),
    "bar": (100000.0, "Pa"),
    # 温度：只收摄氏，开氏需要偏移量而非乘系数，混进来会算错，宁可不收
    "℃": (1.0, "℃"), "°c": (1.0, "℃"), "度": (1.0, "℃"),
    # 其它常见
    "rpm": (1.0, "rpm"), "db": (1.0, "dB"), "dba": (1.0, "dB"),
}

# 数值 + 单位。数值支持小数与千分位逗号；单位取紧随其后的符号/中文。
_NUM = r"[-+]?\d[\d,]*(?:\.\d+)?"
_UNIT = r"[A-Za-z°℃³/]+|[千毫厘]?[瓦米克伏安度]|吨|赫兹"

# 区间：400-600W / 400~600 W / 400 至 600W
_RANGE_RE = re.compile(
    rf"^\s*({_NUM})\s*[-~～至]\s*({_NUM})\s*({_UNIT})?\s*$")
# 单边：≥300CFM / >=300 CFM / ≤50dB / <50dB
_BOUND_RE = re.compile(
    rf"^\s*(≥|>=|>|≤|<=|<)\s*({_NUM})\s*({_UNIT})?\s*$")
# 公差：3.5±0.1mm
_TOL_RE = re.compile(
    rf"^\s*({_NUM})\s*[±+\-]/?[-]?\s*({_NUM})\s*({_UNIT})?\s*$")
# 单值：500W / 1.5 kW / 500 瓦
_SINGLE_RE = re.compile(rf"^\s*({_NUM})\s*({_UNIT})\s*$")


@dataclass(frozen=True)
class ParamValue:
    """一个单元格解析出的量。

    单值用 lo == hi 表示，区间用 lo < hi 表示，单边界另一侧为 None。
    unit 一律是归一后的基准单位——存储与过滤都只认基准单位，
    展示仍用 chunk.text 里的原始表格，两者不互相污染。
    """

    lo: float | None
    hi: float | None
    unit: str


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def normalize_unit(num: float, unit: str) -> tuple[float, str] | None:
    """量纲归一到基准单位；单位不在表内返回 None（不猜）。"""
    key = unit.strip().lower()
    entry = _UNIT_TABLE.get(key)
    if entry is None:
        return None
    factor, base = entry
    return num * factor, base


def parse_cell(raw: str) -> ParamValue | None:
    """单元格文本 → ParamValue；无法确定数值与单位时返回 None。

    **无单位的裸数字一律不收**（如 "500"）：没有单位就没法跨行归一，
    表格里同一列混着 W 和 kW 的情况很常见，按裸数字比大小会错得无声无息。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    m = _RANGE_RE.match(text)
    if m and m.group(3):
        lo, hi, unit = _to_float(m.group(1)), _to_float(m.group(2)), m.group(3)
        if lo is None or hi is None:
            return None
        nlo, nhi = normalize_unit(lo, unit), normalize_unit(hi, unit)
        if nlo is None or nhi is None:
            return None
        return ParamValue(min(nlo[0], nhi[0]), max(nlo[0], nhi[0]), nlo[1])

    m = _TOL_RE.match(text)
    if m and m.group(3) and "±" in text:
        center, tol, unit = _to_float(m.group(1)), _to_float(m.group(2)), m.group(3)
        if center is None or tol is None:
            return None
        n = normalize_unit(center, unit)
        nt = normalize_unit(tol, unit)
        if n is None or nt is None:
            return None
        return ParamValue(n[0] - nt[0], n[0] + nt[0], n[1])

    m = _BOUND_RE.match(text)
    if m and m.group(3):
        op, num, unit = m.group(1), _to_float(m.group(2)), m.group(3)
        if num is None:
            return None
        n = normalize_unit(num, unit)
        if n is None:
            return None
        if op in ("≥", ">=", ">"):
            return ParamValue(n[0], None, n[1])
        return ParamValue(None, n[0], n[1])

    m = _SINGLE_RE.match(text)
    if m:
        num, unit = _to_float(m.group(1)), m.group(2)
        if num is None:
            return None
        n = normalize_unit(num, unit)
        if n is None:
            return None
        return ParamValue(n[0], n[0], n[1])

    return None


def extract_group_params(header: list[str], rows: list[list[str]]) -> dict:
    """行组 → 块级参数区间 {列名: {"min":x, "max":y, "unit":u}}。

    只收录解析成功率达阈值、且**基准单位一致**的列。单位不一致说明该列混了
    不同量纲（或解析串了行），此时宁可整列不收：一个错位的区间会让过滤
    静默漏掉正确答案，比没有过滤更难排查。

    单边界（如 "≥300CFM"）参与区间但不贡献该侧端点——组区间取所有行的
    并集，min 为各行下界的最小值、max 为各行上界的最大值；某行缺某侧
    端点时该侧不参与该行的贡献。
    """
    if not header or not rows:
        return {}

    out: dict[str, dict] = {}
    for col_idx, col_name in enumerate(header):
        name = (col_name or "").strip()
        if not name:
            continue
        parsed: list[ParamValue] = []
        seen = 0
        for row in rows:
            if col_idx >= len(row):
                continue
            cell = (row[col_idx] or "").strip()
            if not cell:
                continue          # 空单元格不计入分母，残缺行不该拉低覆盖率
            seen += 1
            pv = parse_cell(cell)
            if pv is not None:
                parsed.append(pv)
        if seen == 0 or len(parsed) / seen < _COLUMN_COVERAGE_MIN:
            continue
        units = {pv.unit for pv in parsed}
        if len(units) != 1:
            continue
        los = [pv.lo for pv in parsed if pv.lo is not None]
        his = [pv.hi for pv in parsed if pv.hi is not None]
        if not los and not his:
            continue
        out[name] = {
            "min": min(los) if los else None,
            "max": max(his) if his else None,
            "unit": units.pop(),
        }
    return out


# --------------------------------------------------------------------------
# 过滤侧：canonical 范围条件的解释与判定（三条检索路共用，见模块 docstring）
# --------------------------------------------------------------------------

_RANGE_KEYS = ("gte", "lte", "approx", "tol")


def is_range_condition(value) -> bool:
    """判断一个 filters 取值是不是范围条件。

    只有 dict 且含 gte/lte/approx 才算——标量与列表仍走原有等值/集合语义，
    **旧形态行为字节级不变**（ztenith 方案卡与 KMBP 名单库在用）。
    """
    return isinstance(value, dict) and any(k in value for k in _RANGE_KEYS)


def numeric_bounds(cond: dict) -> tuple[float | None, float | None] | None:
    """范围条件 → (lo, hi)。approx/tol 展开成对称区间；非法条件返回 None。

    {"gte":450,"lte":550}          → (450.0, 550.0)
    {"approx":500,"tol":0.1}       → (450.0, 550.0)   tol 为相对比例
    {"gte":300}                    → (300.0, None)
    """
    if not isinstance(cond, dict):
        return None
    if "approx" in cond:
        center = cond.get("approx")
        tol = cond.get("tol", 0.05)
        try:
            c, t = float(center), float(tol)
        except (TypeError, ValueError):
            return None
        if t < 0:
            return None
        return c * (1 - t), c * (1 + t)
    lo = cond.get("gte")
    hi = cond.get("lte")
    if lo is None and hi is None:
        return None
    try:
        return (float(lo) if lo is not None else None,
                float(hi) if hi is not None else None)
    except (TypeError, ValueError):
        return None


def param_field_names(name: str) -> tuple[str, str]:
    """逻辑参数名 → 向量库 payload 里的两个扁平字段名。

    扁平化是为了跟既有 payload 风格一致（`**(doc_meta or {})` 就是平铺的），
    `p_` 前缀防止与文档级 front matter 字段撞名。
    """
    return f"p_{name}_min", f"p_{name}_max"


def group_matches_range(pmin, pmax, lo: float | None, hi: float | None) -> bool:
    """块级区间 [pmin,pmax] 与查询区间 [lo,hi] 是否**重叠**。

    用重叠而非包含：块区间代表"这一组行覆盖的取值范围"，只要有交集就说明
    组内可能存在满足条件的行，应当召回交给 LLM 在组内挑。用包含会把
    "组里有 450~600、查 500" 这种正确命中判负。

    任一侧缺失按无界处理；块完全没有该参数（pmin 与 pmax 都为 None）→ 不匹配，
    与既有"meta 缺字段不匹配"的语义一致。
    """
    if pmin is None and pmax is None:
        return False
    if hi is not None and pmin is not None and pmin > hi:
        return False
    if lo is not None and pmax is not None and pmax < lo:
        return False
    return True


def flatten_params(layout) -> dict:
    """Chunk.layout（dict 或 JSON 字符串）→ 可进向量库 payload 的扁平字段。

    摄取与重建索引两处都必须调用本函数，否则重建后稠密路过滤会静默失效
    ——payload 里没有字段，Qdrant 的 must 条件直接过滤掉全部候选。
    """
    import json

    if layout is None:
        return {}
    if isinstance(layout, str):
        try:
            layout = json.loads(layout) or {}
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(layout, dict):
        return {}
    params = layout.get("params") or {}
    if not isinstance(params, dict):
        return {}
    out: dict = {}
    for name, spec in params.items():
        if not isinstance(spec, dict):
            continue
        fmin, fmax = param_field_names(name)
        if spec.get("min") is not None:
            out[fmin] = spec["min"]
        if spec.get("max") is not None:
            out[fmax] = spec["max"]
    return out


def layout_param_bounds(layout, name: str) -> tuple[float | None, float | None]:
    """从 Chunk.layout 取某参数的 (min, max)；没有该参数返回 (None, None)。"""
    import json

    if layout is None:
        return (None, None)
    if isinstance(layout, str):
        try:
            layout = json.loads(layout) or {}
        except (json.JSONDecodeError, TypeError):
            return (None, None)
    if not isinstance(layout, dict):
        return (None, None)
    spec = (layout.get("params") or {}).get(name)
    if not isinstance(spec, dict):
        return (None, None)
    return spec.get("min"), spec.get("max")
