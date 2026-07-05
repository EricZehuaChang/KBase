import pytest

from kbase.jobs.proposal import generate_outline, parse_outline_json
from kbase.rag.retriever import ContextBlock


# ---- parse_outline_json：鲁棒解析四例 ----

def test_parse_clean_json_array():
    raw = '[{"title": "第一章", "brief": "概述"}, {"title": "第二章", "brief": "细则"}]'
    out = parse_outline_json(raw)
    assert out == [{"title": "第一章", "brief": "概述"},
                   {"title": "第二章", "brief": "细则"}]


def test_parse_json_wrapped_in_fence():
    raw = '```json\n[{"title": "第一章", "brief": "概述"}]\n```'
    out = parse_outline_json(raw)
    assert out == [{"title": "第一章", "brief": "概述"}]


def test_parse_json_with_surrounding_prose():
    raw = ('好的，以下是大纲：\n'
           '[{"title": "第一章", "brief": "概述"}]\n'
           '如需调整请告诉我。')
    out = parse_outline_json(raw)
    assert out == [{"title": "第一章", "brief": "概述"}]


def test_parse_bad_json_raises_value_error_with_excerpt():
    raw = "抱歉，我无法生成大纲。"
    with pytest.raises(ValueError) as exc_info:
        parse_outline_json(raw)
    assert "抱歉" in str(exc_info.value)


# ---- generate_outline：检索背景 + LLM 调用 + 解析集成 ----

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


def _block():
    return ContextBlock(doc_id="d1", doc_name="政策.docx",
                        heading_path="政策.docx > 第一章",
                        text="住房补贴按连续工龄核算。",
                        snippet="住房补贴按连续工龄核算。", score=0.9)


async def test_generate_outline_retrieves_background_and_parses_llm_json():
    retriever = FakeRetriever([_block()])
    llm = FakeLLM('[{"title": "背景", "brief": "说明政策背景"}]')

    outline = await generate_outline(retriever, llm, kb_id="kb1",
                                     topic="住房保障方案", requirements="依据现行政策")

    assert outline == [{"title": "背景", "brief": "说明政策背景"}]
    # 检索以 topic 为 query，top_k=5
    assert retriever.calls == [("kb1", "住房保障方案", 5)]
    # LLM 收到的 prompt 包含检索背景与要求
    prompt_text = " ".join(m["content"] for m in llm.last_messages)
    assert "住房补贴按连续工龄核算" in prompt_text
    assert "依据现行政策" in prompt_text
    assert "住房保障方案" in prompt_text


async def test_generate_outline_raises_on_bad_llm_output():
    retriever = FakeRetriever([_block()])
    llm = FakeLLM("我不太确定应该怎么回答")
    with pytest.raises(ValueError):
        await generate_outline(retriever, llm, kb_id="kb1",
                               topic="住房保障方案", requirements="")
