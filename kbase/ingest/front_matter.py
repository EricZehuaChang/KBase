"""Markdown YAML front matter 解析（ztenith 流水线方案卡摄取，通用能力）。

方案卡是"Markdown 正文 + YAML front matter 元数据"的文件：front matter 进
chunk metadata（供检索元数据过滤），正文照常分块向量化。本模块只做纯文本
解析，容错原则：**解析失败绝不阻塞摄取**——没有 front matter、YAML 语法错、
顶层不是映射，一律按"无元数据"处理，原文整体作为正文返回。

元数据值形状约束（向量库两档的最大公约数）：
- 标量（str/int/float/bool）原样保留；
- 列表压平为标量列表（元素转 str）；
- 嵌套 dict / 其他类型丢弃（向量库 payload 与过滤语义都不支持，静默跳过）。
"""
from __future__ import annotations

import datetime

import yaml

_DELIM = "---"


def split_front_matter(text: str) -> tuple[dict, str]:
    """返回 (metadata, body)。无有效 front matter 时 metadata={}，body=原文。

    front matter 判定：文件以 "---" 独占行开头，且其后存在闭合 "---" 行；
    中间内容 yaml.safe_load 为映射。任何一步不满足都按无元数据处理。"""
    if not text.startswith(_DELIM):
        return {}, text
    lines = text.splitlines(keepends=True)
    # 首行必须恰是 "---"（允许行尾空白/换行）
    if lines[0].strip() != _DELIM:
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == _DELIM:
            raw = "".join(lines[1:i])
            body = "".join(lines[i + 1:])
            try:
                data = yaml.safe_load(raw)
            except yaml.YAMLError:
                return {}, text
            if not isinstance(data, dict):
                return {}, text
            return sanitize_meta(data), body
    return {}, text


def sanitize_meta(data: dict) -> dict:
    """压平为"标量或标量列表"的扁平 dict（见模块 docstring 的形状约束）。
    键统一转 str；列表内的非标量元素丢弃；空列表丢弃。"""
    out: dict = {}
    for k, v in data.items():
        key = str(k)
        if isinstance(v, (str, int, float, bool)):
            out[key] = v
        elif isinstance(v, (datetime.date, datetime.datetime)):
            out[key] = str(v)      # YAML 自动把 2026-08-06 解析成 date 对象
        elif isinstance(v, list):
            vals = [str(x) if not isinstance(x, (str, int, float, bool)) else x
                    for x in v if not isinstance(x, (dict, list))]
            if vals:
                out[key] = vals
    return out
