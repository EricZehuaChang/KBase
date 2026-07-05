import pytest

from kbase.rag.rewriter import QueryRewriter, RewriteResult, should_rewrite

HIST = [{"role": "user", "content": "出差北京住宿费标准是多少？"},
        {"role": "assistant", "content": "部级1100元，司局级650元，其他人员500元。"}]


class FakeLLM:
    def __init__(self, out="出差北京司局级住宿费标准是多少"):
        self.out = out
        self.calls = 0

    async def complete(self, messages, **params):
        self.calls += 1
        return self.out


class BrokenLLM:
    async def complete(self, messages, **params):
        raise RuntimeError("down")


def test_should_rewrite_conditional():
    assert should_rewrite("那司局级呢？", HIST, mode="conditional") is True   # 短+指代
    assert should_rewrite("那司局级呢？", [], mode="conditional") is False     # 无历史
    long_self = "兵团本级机关事业单位工作人员差旅费管理办法中关于住宿费报销的具体标准是什么"
    assert should_rewrite(long_self, HIST, mode="conditional") is False       # 长且与历史有重叠
    assert should_rewrite("公务卡结算范围", HIST, mode="conditional") is True  # 无指代但与历史无重叠且短


def test_should_rewrite_modes():
    assert should_rewrite("那呢", HIST, mode="off") is False
    assert should_rewrite(long_q := "一个完全自包含的很长很长的政策问题" * 3, HIST, mode="always") is True
    assert should_rewrite(long_q, [], mode="always") is False                 # always 也要求有历史


async def test_rewrite_triggered_uses_llm():
    llm = FakeLLM()
    r = QueryRewriter(llm=llm, mode="conditional")
    res = await r.rewrite("那司局级呢？", HIST)
    assert res.triggered and res.rewritten
    assert res.query == "出差北京司局级住宿费标准是多少"
    assert llm.calls == 1


async def test_rewrite_not_triggered_passthrough():
    llm = FakeLLM()
    r = QueryRewriter(llm=llm, mode="off")
    res = await r.rewrite("那司局级呢？", HIST)
    assert res.query == "那司局级呢？" and not res.triggered and llm.calls == 0


async def test_rewrite_failure_falls_back():
    r = QueryRewriter(llm=BrokenLLM(), mode="always")
    res = await r.rewrite("那司局级呢？", HIST)
    assert res.query == "那司局级呢？" and res.triggered and not res.rewritten


async def test_rewrite_empty_output_falls_back():
    r = QueryRewriter(llm=FakeLLM(out="  "), mode="always")
    res = await r.rewrite("那司局级呢？", HIST)
    assert res.query == "那司局级呢？" and not res.rewritten
