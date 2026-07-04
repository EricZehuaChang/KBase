import pytest


def test_init_reads_env_key(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "sk-fake")
    from kbase.plugins.llm.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider(base_url="https://example.com/v1",
                             api_key_env="TEST_LLM_KEY",
                             model="test-model", max_concurrency=2)
    assert p.model == "test-model"


def test_init_missing_env_raises(monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    from kbase.plugins.llm.openai_compat import OpenAICompatProvider
    with pytest.raises(RuntimeError, match="NO_SUCH_KEY"):
        OpenAICompatProvider(base_url="https://example.com/v1",
                             api_key_env="NO_SUCH_KEY", model="m")


@pytest.mark.external
async def test_real_stream():
    """需要 DASHSCOPE_API_KEY 环境变量，验证真实端点流式输出。"""
    from kbase.plugins.llm.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY", model="qwen-turbo")
    text = "".join([t async for t in p.stream(
        [{"role": "user", "content": "回复两个字：你好"}])])
    assert len(text) > 0
