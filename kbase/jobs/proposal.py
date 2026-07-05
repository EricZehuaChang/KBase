"""方案生成流：大纲生成（F2）；逐节生成与汇整（F3，本文件本节起）。"""
import asyncio
import json
import re
from pathlib import Path
from typing import Callable

from kbase.jobs.store import update_job
from kbase.rag.generator import Generator

NO_EVIDENCE_PLACEHOLDER = "（知识库中无相关依据，本节未生成）"

SECTION_QUESTION_TEMPLATE = "请撰写方案章节《{title}》：{brief}（背景主题：{topic}）"

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


async def generate_section(retriever, llm, kb_id: str, topic: str,
                           section: dict) -> dict:
    """单节生成：以"主题+节标题+brief"检索 top-5 → 复用 Generator 的 usable_blocks
    门与 _build_messages/citations 语义（与问答一致的可溯源标准）→ complete 非流式。

    无可用块（检索为空或全部低于拒答门）时不调用 LLM，直接返回占位说明，
    citations 为空列表——与摄取/问答一致的"不编造"哲学延伸到方案生成。
    """
    query = f"{topic} {section['title']} {section['brief']}"
    blocks = retriever.retrieve(kb_id, query, 5)

    gen = Generator(llm)
    usable = gen.usable_blocks(blocks)
    if not usable:
        return {"title": section["title"], "text": NO_EVIDENCE_PLACEHOLDER,
                "citations": []}

    question = SECTION_QUESTION_TEMPLATE.format(
        title=section["title"], brief=section["brief"], topic=topic)
    messages = gen._build_messages(question, usable)
    text = await llm.complete(messages)
    return {"title": section["title"], "text": text,
            "citations": gen.citations(usable)}


_CITATION_REF_RE = re.compile(r"\[(\d+)\]")


def assemble(topic: str, sections_out: list[dict]) -> str:
    """纯函数：汇整各节为最终 Markdown。

    全局引用表按 (doc_name, heading_path) 去重编号（先到先得，按各节出现顺序），
    每节正文中的 [n] 正则替换为该节 citations[n-1] 对应的全局编号；n 超出该节
    citations 范围时防御性保留原样（不重映射、不报错——生成阶段的模型偶发编号
    越界不应该拖垮整份文档的汇整）。
    """
    global_index: dict[tuple[str, str], int] = {}
    global_order: list[tuple[str, str]] = []

    def _global_number(doc_name: str, heading_path: str) -> int:
        key = (doc_name, heading_path)
        if key not in global_index:
            global_order.append(key)
            global_index[key] = len(global_order)
        return global_index[key]

    lines = [f"# {topic}", ""]
    for sec in sections_out:
        lines.append(f"## {sec['title']}")
        citations = sec.get("citations") or []

        def _remap(m: re.Match) -> str:
            n = int(m.group(1))
            if n < 1 or n > len(citations):
                return m.group(0)
            cit = citations[n - 1]
            g = _global_number(cit["doc_name"], cit["heading_path"])
            return f"[{g}]"

        lines.append(_CITATION_REF_RE.sub(_remap, sec["text"]))
        lines.append("")

    lines.append("## 引用文献")
    for i, (doc_name, heading_path) in enumerate(global_order, start=1):
        lines.append(f"[{i}] {doc_name} › {heading_path}")

    return "\n".join(lines)


def build_proposal_steps(sf, retriever, llm, kb_id: str, topic: str,
                         outline: list[dict], job_id: str,
                         jobs_dir) -> list[tuple[str, Callable]]:
    """为 runner 生成步骤列表：每节一步（async 生成包一层 asyncio.run，
    同 ContextualEnricher.enrich 的模式——run_job 由 BackgroundTasks 工作线程
    调用，线程内无运行中的事件循环，可以直接 asyncio.run）+ 汇整一步 +
    写产物一步。三类步骤共享闭包内的 results 列表按节顺序收集结果。

    产物路径：{jobs_dir}/{job_id}/artifact.md。
    """
    results: list[dict] = []
    assembled: dict[str, str] = {}

    def _make_section_step(section: dict) -> Callable:
        def step():
            try:
                out = asyncio.run(generate_section(retriever, llm, kb_id, topic, section))
            except Exception:
                # 单节失败不阻断汇整：留占位说明进入 results，让最终产物仍有该
                # 章节标题；异常继续上抛，交由 runner 标记该步 failed
                # （→ 整体 done_with_errors），与摄取失败隔离哲学一致。
                results.append({"title": section["title"],
                               "text": NO_EVIDENCE_PLACEHOLDER, "citations": []})
                raise
            results.append(out)
            return out["text"]
        return step

    def _assemble_step():
        assembled["md"] = assemble(topic, results)
        return None

    def _write_artifact_step():
        out_dir = Path(jobs_dir) / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "artifact.md"
        out_path.write_text(assembled["md"], encoding="utf-8")
        update_job(sf, job_id, artifact_path=str(out_path))
        return str(out_path)

    steps: list[tuple[str, Callable]] = [
        (f"生成节：{section['title']}", _make_section_step(section))
        for section in outline
    ]
    steps.append(("汇整", _assemble_step))
    steps.append(("写产物", _write_artifact_step))
    return steps
