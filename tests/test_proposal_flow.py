import re

import pytest

from kbase.db import make_session_factory
from kbase.jobs.proposal import assemble, build_proposal_steps, generate_section
from kbase.jobs.runner import run_job
from kbase.jobs.store import create_job, get_job
from kbase.rag.retriever import ContextBlock


# ---- assemble()：纯函数，全局引用重编号 ----

def test_assemble_dedups_same_doc_heading_across_sections():
    """两节引用同一 (doc_name, heading_path) → 合并为同一个全局编号。"""
    sections_out = [
        {"title": "第一章", "text": "住房补贴按工龄核算[1]。",
         "citations": [{"doc_name": "政策.docx", "heading_path": "政策.docx > 第一章",
                        "snippet": "住房补贴按工龄核算。"}]},
        {"title": "第二章", "text": "同样依据该政策[1]执行。",
         "citations": [{"doc_name": "政策.docx", "heading_path": "政策.docx > 第一章",
                        "snippet": "住房补贴按工龄核算。"}]},
    ]
    md = assemble("住房保障方案", sections_out)

    assert "# 住房保障方案" in md
    assert "## 第一章" in md and "## 第二章" in md
    assert "住房补贴按工龄核算[1]。" in md
    assert "同样依据该政策[1]执行。" in md
    # 引用文献只出现一条（去重）
    assert md.count("政策.docx › 政策.docx > 第一章") == 1


def test_assemble_different_headings_increment_global_number():
    """不同出处 → 全局编号递增。"""
    sections_out = [
        {"title": "第一章", "text": "依据甲文件[1]。",
         "citations": [{"doc_name": "甲.docx", "heading_path": "甲.docx > 一",
                        "snippet": "甲文件内容"}]},
        {"title": "第二章", "text": "依据乙文件[1]。",
         "citations": [{"doc_name": "乙.docx", "heading_path": "乙.docx > 一",
                        "snippet": "乙文件内容"}]},
    ]
    md = assemble("方案", sections_out)

    # 第二节原始 [1] 应重映射为全局 [2]（甲=1 先出现，乙=2 后出现）
    assert "依据甲文件[1]。" in md
    assert "依据乙文件[2]。" in md
    assert "[1] 甲.docx › 甲.docx > 一" in md
    assert "[2] 乙.docx › 乙.docx > 一" in md


def test_assemble_citation_index_out_of_range_left_as_is():
    """正文引用的编号超出该节 citations 列表范围 → 防御性保留原样，不重映射。"""
    sections_out = [
        {"title": "第一章", "text": "参见[3]的说明。",
         "citations": [{"doc_name": "甲.docx", "heading_path": "甲.docx > 一",
                        "snippet": "甲文件内容"}]},
    ]
    md = assemble("方案", sections_out)
    assert "参见[3]的说明。" in md


def test_assemble_appendix_heading_present():
    sections_out = [
        {"title": "第一章", "text": "无引用文本。", "citations": []},
    ]
    md = assemble("方案", sections_out)
    assert "## 引用文献" in md


# ---- generate_section()：usable 门 + 无依据占位 ----

class FakeRetriever:
    def __init__(self, blocks):
        self.blocks = blocks
        self.calls = []

    def retrieve(self, kb_id, query, top_k=5):
        self.calls.append((kb_id, query, top_k))
        return self.blocks


class FakeLLM:
    def __init__(self, out):
        self.out = out
        self.last_messages = None

    async def complete(self, messages, **params):
        self.last_messages = messages
        return self.out


def _block(score=0.9):
    return ContextBlock(doc_id="d1", doc_name="政策.docx",
                        heading_path="政策.docx > 第一章",
                        text="住房补贴按连续工龄核算。",
                        snippet="住房补贴按连续工龄核算。", score=score)


async def test_generate_section_no_usable_blocks_returns_placeholder():
    retriever = FakeRetriever([])   # 无检索结果 -> usable_blocks 为空
    llm = FakeLLM("不应被调用")
    section = {"title": "背景", "brief": "说明背景"}

    out = await generate_section(retriever, llm, kb_id="kb1", topic="住房保障方案",
                                 section=section)

    assert out["title"] == "背景"
    assert out["text"] == "（知识库中无相关依据，本节未生成）"
    assert out["citations"] == []


async def test_generate_section_low_score_blocks_returns_placeholder():
    """全部块分数低于拒答门 -> usable_blocks 为空 -> 占位（而不是报错）。"""
    retriever = FakeRetriever([_block(score=0.01)])
    llm = FakeLLM("不应被调用")
    section = {"title": "背景", "brief": "说明背景"}

    out = await generate_section(retriever, llm, kb_id="kb1", topic="住房保障方案",
                                 section=section)

    assert out["text"] == "（知识库中无相关依据，本节未生成）"
    assert out["citations"] == []


async def test_generate_section_retrieves_with_composed_query_and_builds_citations():
    retriever = FakeRetriever([_block()])
    llm = FakeLLM("住房补贴按连续工龄核算[1]。")
    section = {"title": "背景", "brief": "说明背景"}

    out = await generate_section(retriever, llm, kb_id="kb1", topic="住房保障方案",
                                 section=section)

    # 检索 query 组合 topic + section title + brief
    assert retriever.calls == [("kb1", "住房保障方案 背景 说明背景", 5)]
    assert out["title"] == "背景"
    assert out["text"] == "住房补贴按连续工龄核算[1]。"
    assert len(out["citations"]) == 1
    assert out["citations"][0]["doc_name"] == "政策.docx"
    assert out["citations"][0]["heading_path"] == "政策.docx > 第一章"
    # LLM 收到组装好的 messages，包含节标题/brief/主题的组合 prompt
    prompt_text = " ".join(m["content"] for m in llm.last_messages)
    assert "背景" in prompt_text and "说明背景" in prompt_text
    assert "住房保障方案" in prompt_text


# ---- build_proposal_steps + run_job：全流程集成 ----

def _sf(tmp_path):
    return make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")


def test_full_proposal_flow_produces_artifact_with_global_citations(tmp_path):
    sf = _sf(tmp_path)
    job = create_job(sf, kb_id="kb1", type="proposal",
                      params={"topic": "住房保障方案"}, provider=None)

    retriever = FakeRetriever([_block()])
    llm = FakeLLM("住房补贴按连续工龄核算[1]。")
    outline = [{"title": "第一章 背景", "brief": "说明背景"},
               {"title": "第二章 依据", "brief": "说明政策依据"}]
    jobs_dir = tmp_path / "jobs"

    steps = build_proposal_steps(sf, retriever, llm, kb_id="kb1",
                                 topic="住房保障方案", outline=outline,
                                 job_id=job["id"], jobs_dir=jobs_dir)
    run_job(sf, job["id"], steps)

    got = get_job(sf, job["id"])
    assert got["status"] == "done"
    assert got["artifact_path"]

    artifact_path = got["artifact_path"]
    md = open(artifact_path, encoding="utf-8").read()

    assert "# 住房保障方案" in md
    assert "## 第一章 背景" in md
    assert "## 第二章 依据" in md
    assert "住房补贴按连续工龄核算[1]。" in md
    assert "## 引用文献" in md
    assert "[1] 政策.docx › 政策.docx > 第一章" in md


class PartiallyFailingRetriever:
    """检索按 query 前缀分流：第一章的检索抛异常（模拟下游服务不可用），
    其余节检索正常返回可用块——用于固定"某节生成失败、其余节正常"的场景。"""

    def __init__(self, blocks):
        self.blocks = blocks

    def retrieve(self, kb_id, query, top_k=5):
        if "第一章" in query:
            raise RuntimeError("检索服务不可用")
        return self.blocks


def test_full_proposal_flow_section_failure_gets_placeholder_and_done_with_errors(tmp_path):
    """一节检索抛异常 -> 该步 failed，但 results 仍收到该节的无依据占位，
    最终产物包含该节标题 + 占位文本，job 整体状态 done_with_errors（其余步骤
    仍成功、有产出）——固定这一已被 ad-hoc 验证过的失败隔离路径为回归测试。"""
    sf = _sf(tmp_path)
    job = create_job(sf, kb_id="kb1", type="proposal",
                      params={"topic": "住房保障方案"}, provider=None)

    retriever = PartiallyFailingRetriever([_block()])
    llm = FakeLLM("住房补贴按连续工龄核算[1]。")
    outline = [{"title": "第一章 背景", "brief": "说明背景"},
               {"title": "第二章 依据", "brief": "说明政策依据"}]
    jobs_dir = tmp_path / "jobs"

    steps = build_proposal_steps(sf, retriever, llm, kb_id="kb1",
                                 topic="住房保障方案", outline=outline,
                                 job_id=job["id"], jobs_dir=jobs_dir)
    run_job(sf, job["id"], steps)

    got = get_job(sf, job["id"])
    assert got["status"] == "done_with_errors"
    assert got["artifact_path"]

    steps_state = got["progress"]["steps"]
    statuses = {s["name"]: s["status"] for s in steps_state}
    assert statuses["生成节：第一章 背景"] == "failed"
    assert statuses["生成节：第二章 依据"] == "done"

    md = open(got["artifact_path"], encoding="utf-8").read()
    assert "## 第一章 背景" in md
    assert "（知识库中无相关依据，本节未生成）" in md
    assert "## 第二章 依据" in md
    assert "住房补贴按连续工龄核算[1]。" in md
