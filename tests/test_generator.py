import pytest

from kbase.rag.generator import MIN_SCORE, Generator
from kbase.rag.retriever import ContextBlock


class FakeLLM:
    """回显收到的 user prompt，便于断言 prompt 组装。"""
    def __init__(self):
        self.last_messages = None

    async def stream(self, messages, **params):
        self.last_messages = messages
        for piece in ["根据资料[1]，", "满两年可申领。"]:
            yield piece


def _block(score=0.9):
    return ContextBlock(doc_id="d1", doc_name="补贴办法.docx",
                        heading_path="补贴办法.docx > 第一章",
                        text="连续工作满两年可申领住房补贴。",
                        snippet="满两年可申领", score=score)


async def test_stream_with_citations():
    llm = FakeLLM()
    gen = Generator(llm)
    chunks = [c async for c in gen.answer_stream("申领条件是什么", [_block()])]
    assert "".join(chunks) == "根据资料[1]，满两年可申领。"
    user_prompt = llm.last_messages[-1]["content"]
    assert "[1]" in user_prompt and "连续工作满两年" in user_prompt
    assert "申领条件是什么" in user_prompt
    cits = gen.citations([_block()])
    assert cits[0]["index"] == 1 and cits[0]["doc_name"] == "补贴办法.docx"


async def test_refusal_when_no_context():
    gen = Generator(FakeLLM())
    chunks = [c async for c in gen.answer_stream("随便问", [])]
    assert "未找到依据" in "".join(chunks)


async def test_refusal_when_low_score():
    gen = Generator(FakeLLM())
    low = _block(score=MIN_SCORE - 0.01)
    chunks = [c async for c in gen.answer_stream("随便问", [low])]
    assert "未找到依据" in "".join(chunks)


async def test_citations_use_usable_blocks_only():
    """citations() 和 answer_stream() 必须传入同一份经 usable_blocks() 过滤后的列表，
    否则引用编号可能与回答中的 [n] 标记错位。"""
    gen = Generator(FakeLLM())
    high = _block(score=0.9)
    low = _block(score=MIN_SCORE - 0.01)
    mixed = [high, low]
    usable = gen.usable_blocks(mixed)
    assert usable == [high]
    cits = gen.citations(usable)
    assert len(cits) == 1
    assert cits[0]["index"] == 1
    assert cits[0]["doc_name"] == "补贴办法.docx"
