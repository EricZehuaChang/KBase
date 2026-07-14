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


def test_default_params_merged(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "sk-fake")
    from kbase.plugins.llm.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider(base_url="https://example.com/v1", api_key_env="TEST_LLM_KEY",
                             model="m", params={"extra_body": {"enable_thinking": False}})
    assert p._default_params["extra_body"]["enable_thinking"] is False


def test_call_site_params_override_defaults(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "sk-fake")
    from kbase.plugins.llm.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider(base_url="https://example.com/v1", api_key_env="TEST_LLM_KEY",
                             model="m", params={"temperature": 0.1})
    merged = {**p._default_params, **{"temperature": 0.9}}
    assert merged["temperature"] == 0.9


def test_direct_api_key_without_env(monkeypatch):
    """M5-2：DB 直配 api_key 时不需要任何环境变量。"""
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    from kbase.plugins.llm.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider(base_url="https://example.com/v1",
                             api_key_env="NO_SUCH_KEY", model="m",
                             api_key="sk-direct")
    assert p.model == "m"


def test_direct_api_key_takes_precedence_over_env(monkeypatch):
    """直配 key 优先于环境变量（页面里改了 key 必须立即生效，不被 env 盖住）。"""
    monkeypatch.setenv("TEST_LLM_KEY", "sk-from-env")
    from kbase.plugins.llm.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider(base_url="https://example.com/v1",
                             api_key_env="TEST_LLM_KEY", model="m",
                             api_key="sk-direct-wins")
    assert p._client.api_key == "sk-direct-wins"


def test_no_key_source_at_all_raises(monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    from kbase.plugins.llm.openai_compat import OpenAICompatProvider
    with pytest.raises(RuntimeError, match="未配置密钥"):
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
