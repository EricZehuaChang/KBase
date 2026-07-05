"""定期汇编流：对选定文档集逐文档摘要 + 总览 → 汇整 Markdown（F4）。

与 proposal.py 的 build_proposal_steps 同模式：每份文档一步生成摘要，
最后总览步 + 汇整步 + 写产物步；单文档失败（如 content.md 缺失）不阻断
其余文档，交由 runner 的失败隔离语义处理（→ done_with_errors）。
"""
import asyncio
from pathlib import Path
from typing import Callable

from kbase.jobs.store import update_job
from kbase.models import Document

SUMMARY_CONTENT_CHARS = 6000

SUMMARY_SYSTEM_PROMPT = (
    "你是一个严谨的文档摘要助手。根据给定的文档正文，写一段不超过200字的摘要，"
    "只输出摘要正文，不要标题、不要解释性文字。"
)

SUMMARY_USER_TEMPLATE = """文档名：{filename}

正文（节选）：
{content}

请输出该文档的摘要（200字以内）。"""

OVERVIEW_SYSTEM_PROMPT = (
    "你是一个严谨的文档汇编助手。根据以下各文档摘要，写一段总览性的文字，"
    "概括这批文档整体涉及的主题与要点。只输出总览正文，不要标题、不要解释性文字。"
)

OVERVIEW_USER_TEMPLATE = """各文档摘要如下：

{summaries}

请输出总览段落。"""


async def summarize_document(llm, filename: str, content: str) -> str:
    """截取正文前 SUMMARY_CONTENT_CHARS 字 → LLM 生成摘要（≤200字，固定 prompt）。"""
    truncated = content[:SUMMARY_CONTENT_CHARS]
    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": SUMMARY_USER_TEMPLATE.format(
            filename=filename, content=truncated)},
    ]
    return await llm.complete(messages)


async def generate_overview(llm, summaries: list[dict]) -> str:
    """各文档摘要合并请求一段总览。"""
    joined = "\n\n".join(f"《{s['filename']}》：{s['summary']}" for s in summaries)
    messages = [
        {"role": "system", "content": OVERVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": OVERVIEW_USER_TEMPLATE.format(summaries=joined)},
    ]
    return await llm.complete(messages)


def assemble_digest(kb_name: str, overview: str, summaries: list[dict]) -> str:
    """纯函数：汇整为最终 Markdown。
    `# {kb_name}文档汇编` + 总览 section + 每文档 `## {filename}` 摘要 section。
    """
    lines = [f"# {kb_name}文档汇编", "", "## 总览", "", overview, ""]
    for s in summaries:
        lines.append(f"## {s['filename']}")
        lines.append("")
        lines.append(s["summary"])
        lines.append("")
    return "\n".join(lines)


def _resolve_docs(sf, kb_id: str, doc_ids: list[str] | None) -> list[dict]:
    """解析文档列表：doc_ids 指定则按其取（保序）；否则取该 kb 全部 ready 文档。"""
    with sf() as s:
        if doc_ids:
            docs = []
            for doc_id in doc_ids:
                doc = s.get(Document, doc_id)
                if doc is not None:
                    docs.append({"id": doc.id, "filename": doc.filename})
            return docs
        rows = (s.query(Document).filter_by(kb_id=kb_id, status="ready")
                .order_by(Document.created_at.asc()).all())
        return [{"id": d.id, "filename": d.filename} for d in rows]


def build_digest_steps(sf, llm, kb_id: str, doc_ids: list[str] | None, job_id: str,
                       files_dir, jobs_dir, kb_name: str) -> list[tuple[str, Callable]]:
    """为 runner 生成步骤列表：解析文档列表（doc_ids 或全库 ready 文档）→
    每文档一步读 content.md 前 6000 字生成摘要（缺失 content.md 该步失败，
    runner 继续后续步骤）→ 总览步 → 汇整步 → 写产物步。

    产物路径：{jobs_dir}/{job_id}/artifact.md。
    """
    docs = _resolve_docs(sf, kb_id, doc_ids)
    files_dir = Path(files_dir)

    summaries: list[dict] = []
    assembled: dict[str, str] = {}

    def _make_summary_step(doc: dict) -> Callable:
        def step():
            content_path = files_dir / doc["id"] / "content.md"
            if not content_path.exists():
                raise FileNotFoundError(f"文档正文不存在: {content_path}")
            content = content_path.read_text(encoding="utf-8")
            summary = asyncio.run(summarize_document(llm, doc["filename"], content))
            summaries.append({"filename": doc["filename"], "summary": summary})
            return summary
        return step

    def _overview_step():
        overview = asyncio.run(generate_overview(llm, summaries))
        assembled["overview"] = overview
        return overview

    def _assemble_step():
        assembled["md"] = assemble_digest(kb_name, assembled.get("overview", ""), summaries)
        return None

    def _write_artifact_step():
        out_dir = Path(jobs_dir) / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "artifact.md"
        out_path.write_text(assembled["md"], encoding="utf-8")
        update_job(sf, job_id, artifact_path=str(out_path))
        return str(out_path)

    steps: list[tuple[str, Callable]] = [
        (f"摘要：{doc['filename']}", _make_summary_step(doc)) for doc in docs
    ]
    steps.append(("总览", _overview_step))
    steps.append(("汇整", _assemble_step))
    steps.append(("写产物", _write_artifact_step))
    return steps
