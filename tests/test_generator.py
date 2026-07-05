import pytest

from kbase.rag.generator import Generator
from kbase.rag.retriever import ContextBlock

MIN_SCORE = 0.3   # 与 Generator 默认 min_score 保持一致，供既有用例复用


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
    assert cits[0]["doc_id"] == "d1"   # 前端"查看文档全文"依赖 doc_id 直取


async def test_refusal_when_no_context():
    gen = Generator(FakeLLM())
    chunks = [c async for c in gen.answer_stream("随便问", [])]
    assert "未找到依据" in "".join(chunks)


async def test_refusal_when_low_score():
    """拒答门看最高分：全部块都低于 min_score → 拒答（行为与改造前一致）。"""
    gen = Generator(FakeLLM())
    low = _block(score=MIN_SCORE - 0.01)
    chunks = [c async for c in gen.answer_stream("随便问", [low])]
    assert "未找到依据" in "".join(chunks)


def test_include_floor_keeps_conflicting_evidence():
    """收录底线与拒答门分离：最高分过门后，低分但 >= min_include_score 的块
    一并收录（可能载有冲突/佐证信息，交给模型辨析）；低于底线的噪声块剔除。"""
    gen = Generator(FakeLLM(), min_score=0.35, min_include_score=0.1)
    b_high, b_mid, b_noise = _block(0.9), _block(0.2), _block(0.05)
    usable = gen.usable_blocks([b_high, b_mid, b_noise])
    assert usable == [b_high, b_mid]        # 0.05 低于底线剔除，0.2 过底线保留


async def test_citations_use_usable_blocks_only():
    """citations() 和 answer_stream() 必须传入同一份经 usable_blocks() 过滤后的列表，
    否则引用编号可能与回答中的 [n] 标记错位。"""
    gen = Generator(FakeLLM(), min_score=MIN_SCORE, min_include_score=0.1)
    high = _block(score=0.9)
    low_but_included = _block(score=MIN_SCORE - 0.01)   # 过底线：收录
    noise = _block(score=0.05)                          # 低于底线：剔除
    usable = gen.usable_blocks([high, low_but_included, noise])
    assert usable == [high, low_but_included]
    cits = gen.citations(usable)
    assert len(cits) == 2
    assert [c["index"] for c in cits] == [1, 2]
    assert cits[0]["doc_name"] == "补贴办法.docx"
