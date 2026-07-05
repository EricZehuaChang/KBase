"""方案生成流：大纲生成（本文件 F2 范围）；逐节生成与汇整见 F3。"""
import json
import re

OUTLINE_SYSTEM_PROMPT = (
    "你是一个严谨的公文/方案撰写助手。根据用户给出的主题、要求与背景资料，"
    "设计一份方案大纲，分为若干章节。只输出 JSON 数组，不要任何解释性文字，"
    "格式为 [{\"title\": \"章节标题\", \"brief\": \"该章节要点简述\"}, ...]。"
)

OUTLINE_USER_TEMPLATE = """主题：{topic}

要求：{requirements}

背景资料（供参考，非必须逐条引用）：
{background}

请输出大纲 JSON 数组。"""


def parse_outline_json(raw: str) -> list[dict]:
    """鲁棒解析 LLM 输出的大纲 JSON：
    1. 剥离 ```json ... ``` 或 ``` ... ``` 围栏；
    2. 取第一个 [ 到最后一个 ] 之间的子串（容忍前后杂文）；
    3. json.loads 失败则抛 ValueError，附带原文摘录便于排查。
    """
    text = raw.strip()
    fence_match = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        excerpt = raw[:200]
        raise ValueError(f"LLM 输出中未找到 JSON 数组，原文摘录: {excerpt!r}")

    candidate = text[start:end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as e:
        excerpt = raw[:200]
        raise ValueError(f"大纲 JSON 解析失败: {e}，原文摘录: {excerpt!r}") from e

    if not isinstance(parsed, list):
        excerpt = raw[:200]
        raise ValueError(f"大纲 JSON 应为数组，原文摘录: {excerpt!r}")

    return parsed


async def generate_outline(retriever, llm, kb_id: str, topic: str,
                           requirements: str) -> list[dict]:
    """检索 topic 相关 top-5 块作为背景 → LLM 一次调用生成 JSON 大纲 → 鲁棒解析。
    返回 [{"title": ..., "brief": ...}, ...]。"""
    blocks = retriever.retrieve(kb_id, topic, 5)
    background = "\n\n".join(
        f"[{i + 1}] 出处：{b.heading_path}\n{b.text}" for i, b in enumerate(blocks)
    ) if blocks else "（知识库中未检索到强相关内容）"

    messages = [
        {"role": "system", "content": OUTLINE_SYSTEM_PROMPT},
        {"role": "user", "content": OUTLINE_USER_TEMPLATE.format(
            topic=topic, requirements=requirements or "无特殊要求",
            background=background)},
    ]
    raw = await llm.complete(messages)
    return parse_outline_json(raw)
