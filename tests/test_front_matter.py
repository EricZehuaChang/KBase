"""方案卡 front matter 解析（ztenith 流水线摄取侧）：容错原则=解析失败绝不
阻塞摄取，一律按"无元数据"处理返回原文。"""
from kbase.ingest.front_matter import sanitize_meta, split_front_matter

CARD = """---
id: card-001
industry: 零售
data_entities: [订单, 库存]
client_size: 500
updated: 2026-08-06
---
# 电商对接方案

正文内容。
"""


def test_split_valid_front_matter():
    meta, body = split_front_matter(CARD)
    assert meta["id"] == "card-001" and meta["industry"] == "零售"
    assert meta["data_entities"] == ["订单", "库存"]
    assert meta["client_size"] == 500
    assert meta["updated"] == "2026-08-06"      # YAML date 对象转回 str
    assert body.startswith("# 电商对接方案")
    assert "---" not in body


def test_no_front_matter_returns_original():
    text = "# 普通文档\n\n没有元数据。"
    meta, body = split_front_matter(text)
    assert meta == {} and body == text


def test_unclosed_or_invalid_yaml_treated_as_body():
    unclosed = "---\nid: x\n正文没有闭合线"
    assert split_front_matter(unclosed) == ({}, unclosed)
    bad_yaml = "---\n: : :\n---\n正文"
    meta, body = split_front_matter(bad_yaml)
    assert meta == {} and body == bad_yaml
    # 顶层不是映射（如列表）同样按无元数据处理
    not_map = "---\n- a\n- b\n---\n正文"
    assert split_front_matter(not_map) == ({}, not_map)


def test_sanitize_drops_nested_and_empty():
    out = sanitize_meta({
        "ok": "v", "n": 3, "b": True,
        "list": ["a", 1],
        "nested": {"x": 1},          # 嵌套 dict 丢弃
        "mixed_list": ["a", {"x": 1}],   # 列表内 dict 元素丢弃
        "empty": [],                 # 空列表丢弃
    })
    assert out == {"ok": "v", "n": 3, "b": True,
                   "list": ["a", 1], "mixed_list": ["a"]}
