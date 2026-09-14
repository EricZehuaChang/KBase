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


# ---- T09：流式真实用量（stream_options.include_usage） ----------------------
#
# 实测口径（DashScope 兼容端点 / qwen-plus，2026-09-14）：流式带
# stream_options={"include_usage": True} 时，末块（choices 为空、只带 usage）
# 回传 prompt=14/completion=1/total=15，与非流式一致。
# 下面用假 client 复刻这一形状：不联网，只验证 provider 侧的三件事——
# ①请求带了 stream_options；②末块 usage 被回填；③末块不会被当成内容 yield
# （否则 SSE 客户端会收到一个空 token 事件）。

class _FakeDelta:
    def __init__(self, content=None):
        self.content = content


class _FakeChoice:
    def __init__(self, content=None):
        self.delta = _FakeDelta(content)


class _FakeChunk:
    def __init__(self, content=None, choices_empty=False, usage=None):
        self.choices = [] if choices_empty else [_FakeChoice(content)]
        self.usage = usage


class _FakeUsage:
    def __init__(self, prompt, completion, total):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


class _FakeCreate:
    """记录每次 create 的 kwargs；可配置前 N 次调用抛错（模拟端点不认参数）。"""

    def __init__(self, chunks, fail_times=0, exc=None):
        self.chunks = chunks
        self.fail_times = fail_times
        self.exc = exc
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_times:
            raise self.exc
        return _FakeStream(self.chunks)


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()


class _BadRequest(Exception):
    """模拟"端点不接受 stream_options"的参数类 400。

    报文必须带 stream_options / include_usage 字样——provider 的降级判定
    （_looks_like_param_rejection）就是按这个信号认的参数类错误；不带字样
    的异常被当作非参数错误直接抛出，不会重试（见该函数的 docstring）。
    真实 OpenAI SDK 对应 openai.BadRequestError，也已在该判定里识别。
    """
    status_code = 400


def _provider_with_fake_create(create) -> object:
    """构造真实 provider（不联网），只把 _client.chat.completions.create 换成假的。

    provider 的 __init__ 会真建 AsyncOpenAI（不发请求），故支持直接替换
    _client.chat.completions.create —— 不触碰真实网络。
    """
    from kbase.plugins.llm.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider(base_url="https://example.com/v1",
                             api_key="sk-fake", model="m")
    p._client.chat.completions.create = create
    return p


async def test_stream_captures_final_chunk_usage_without_leaking_empty_token():
    """T09 回归点：末块 usage 必须被捕获，且**不能**变成空 token 事件。

    最容易坏的方式有两种，本测试同时钉住：
    ① 只按 choices 取内容、把没有 choices 的末块连同 usage 一起丢掉 → 用量丢失；
    ② 把末块当"空内容"照样 yield → 流里多一个空片段（前端渲染空 token）。
    """
    create = _FakeCreate([
        _FakeChunk("满"),
        _FakeChunk("两年"),
        # 末块：choices 为空、只带 usage（实测形状）
        _FakeChunk(choices_empty=True, usage=_FakeUsage(14, 1, 15)),
    ])
    p = _provider_with_fake_create(create)

    got: list[dict] = []
    pieces = [t async for t in p.stream([{"role": "user", "content": "hi"}],
                                        usage_sink=got.append)]

    assert pieces == ["满", "两年"]           # 无空片段（末块未被 yield）
    assert got == [{"prompt_tokens": 14, "completion_tokens": 1,
                    "total_tokens": 15}]
    sent = create.calls[0]
    assert sent["stream_options"] == {"include_usage": True}
    assert "usage_sink" not in sent           # KBase 侧通道不得进上游请求体
    assert sent["stream"] is True


async def test_stream_without_sink_stays_byte_identical():
    """不传 usage_sink 的调用方（/api 问答、分享链接…）行为不变：
    既不向上游请求用量，也不因为末块而多出空片段。"""
    create = _FakeCreate([_FakeChunk("甲"), _FakeChunk("乙")])
    p = _provider_with_fake_create(create)
    pieces = [t async for t in p.stream([{"role": "user", "content": "hi"}])]
    assert pieces == ["甲", "乙"]
    assert "stream_options" not in create.calls[0]


async def test_stream_retries_without_stream_options_when_endpoint_rejects_it():
    """端点不认 stream_options（部分兼容实现回 400）时降级重试一次：
    回答照常产出，只是拿不到真实用量（调用方的 sink 不触发 → 上层回退估算），
    绝不因为计量参数把整个问答打挂。"""
    create = _FakeCreate([_FakeChunk("好")], fail_times=1,
                         exc=_BadRequest("400 invalid_request_error: "
                                         "unknown parameter: stream_options"))
    p = _provider_with_fake_create(create)

    got: list[dict] = []
    pieces = [t async for t in p.stream([{"role": "user", "content": "hi"}],
                                        usage_sink=got.append)]

    assert pieces == ["好"]
    assert got == []                           # 无真实用量 → 上层估算
    assert len(create.calls) == 2
    assert create.calls[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in create.calls[1]     # 重试去掉了该参数


async def test_stream_retry_failure_propagates():
    """降级重试也失败时照常抛错（不吞异常——上游真故障要让调用方看见）。"""
    create = _FakeCreate([], fail_times=2,
                         exc=_BadRequest("400 invalid_request_error: "
                                         "unknown parameter: stream_options"))
    p = _provider_with_fake_create(create)
    with pytest.raises(_BadRequest):
        _ = [t async for t in p.stream([{"role": "user", "content": "hi"}],
                                       usage_sink=lambda u: None)]
    assert len(create.calls) == 2


def _sdk_status_error(name: str, status: int, message: str | None = None):
    """构造 openai SDK 的**真实异常实例**（带 HTTP 响应）——production 路径上
    端点回 400 时 SDK 抛的就是 openai.BadRequestError，判定按 isinstance 走，
    与"错误文本恰好提到参数名"是两条不同的信号，必须分别钉住。"""
    import httpx
    import openai
    req = httpx.Request("POST", "https://example.com/v1/chat/completions")
    return getattr(openai, name)(message or f"{status} from upstream",
                                response=httpx.Response(status, request=req),
                                body=None)


# 降级判定（provider._looks_like_param_rejection）的三条正向信号与
# 三类必须**不**降级的错误。分开参数化：正向漏一条=计量在某些端点永久失效；
# 负向漏一条=把真实故障（限流/鉴权/网络）当参数问题重试，白打一次上游。
_RETRY_SIGNALS = [
    pytest.param(lambda: _sdk_status_error("BadRequestError", 400),
                 id="sdk-bad-request-400"),
    pytest.param(lambda: RuntimeError("400 invalid_request_error: unknown "
                                      "parameter: stream_options"),
                 id="error-text-names-the-param"),
    pytest.param(lambda: TypeError("create() got an unexpected keyword argument "
                                   "'stream_options'"),
                 id="typeerror-unknown-kwarg"),
]
_NO_RETRY_ERRORS = [
    pytest.param(lambda: _sdk_status_error("RateLimitError", 429), id="sdk-429"),
    pytest.param(lambda: _sdk_status_error("AuthenticationError", 401), id="sdk-401"),
    pytest.param(lambda: RuntimeError("upstream timeout"), id="opaque-runtime-error"),
]


@pytest.mark.parametrize("make_exc", _RETRY_SIGNALS)
async def test_stream_retries_on_each_param_rejection_signal(make_exc):
    """参数类错误的三种信号各自都能触发一次降级重试：SDK 的 BadRequestError
    （400 的主要载体）、错误文本点名 stream_options/include_usage（网关把错误
    包成普通异常的端点）、TypeError（SDK/端点不接受该关键字）。"""
    create = _FakeCreate([_FakeChunk("好")], fail_times=1, exc=make_exc())
    p = _provider_with_fake_create(create)

    got: list[dict] = []
    pieces = [t async for t in p.stream([{"role": "user", "content": "hi"}],
                                        usage_sink=got.append)]

    assert pieces == ["好"]                    # 降级后回答照常
    assert got == []                           # 无真实用量 → 上层回退估算
    assert len(create.calls) == 2
    assert create.calls[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in create.calls[1]


@pytest.mark.parametrize("make_exc", _NO_RETRY_ERRORS)
async def test_stream_does_not_retry_non_param_errors(make_exc):
    """非参数类错误**不**降级重试：去掉 stream_options 也救不回来，重试只会
    白打一次上游（429 时尤其糟——等于在限流时立刻再来一发），且会掩盖真实
    故障类型。这是 _looks_like_param_rejection 的负向契约。"""
    exc = make_exc()
    create = _FakeCreate([_FakeChunk("x")], fail_times=99, exc=exc)
    p = _provider_with_fake_create(create)

    with pytest.raises(type(exc)):
        _ = [t async for t in p.stream([{"role": "user", "content": "hi"}],
                                       usage_sink=lambda u: None)]
    assert len(create.calls) == 1              # 只打了一次，没有重试


def _sse_bytes() -> bytes:
    """DashScope 实测形状的流式响应体：两个内容块 + 末块（choices 为空 + usage）。"""
    import json as _json

    def _chunk(content=None, usage=None):
        return _json.dumps({
            "id": "chatcmpl-x", "object": "chat.completion.chunk",
            "created": 0, "model": "m",
            "choices": ([] if content is None else
                        [{"index": 0, "delta": {"content": content},
                          "finish_reason": None}]),
            **({"usage": usage} if usage else {}),
        })

    sse = "\n\n".join([
        "data: " + _chunk("满"),
        "data: " + _chunk("两年"),
        "data: " + _chunk(usage={"prompt_tokens": 14, "completion_tokens": 1,
                                 "total_tokens": 15}),
        "data: [DONE]", ""])
    return sse.encode()


def _provider_with_real_sdk(seen: list[dict]):
    """构造 provider，但把 http 层换成 httpx MockTransport——走**真实 openai
    SDK** 的请求构造与响应解析，只是不出网。seen 收集真实发出的请求体 JSON。"""
    import json as _json

    import httpx
    from openai import AsyncOpenAI

    from kbase.plugins.llm.openai_compat import OpenAICompatProvider

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse_bytes())

    p = OpenAICompatProvider(base_url="https://example.com/v1",
                             api_key="sk-fake", model="m")
    p._client = AsyncOpenAI(
        base_url="https://example.com/v1", api_key="sk-fake",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return p


async def test_stream_usage_via_real_sdk_parsing():
    """同一件事走**真实 openai SDK 的解析路径**（httpx MockTransport 回真 SSE
    字节，不联网）：末块 usage 被 SDK 解析成 CompletionUsage 后回填 sink，且
    空 choices 的末块不会变成空内容。

    与上一个用例的分工：上一个用假 chunk 对象钉死 provider 的处理逻辑，这一个
    钉死"真实 SDK 的字段名与形状"——`u.prompt_tokens` 这类属性访问、usage 块
    choices 为空这两个前提一旦变化（SDK 升级/端点差异），只有这一层能发现。
    """
    seen: list[dict] = []
    p = _provider_with_real_sdk(seen)

    got: list[dict] = []
    pieces = [t async for t in p.stream([{"role": "user", "content": "hi"}],
                                        usage_sink=got.append)]

    assert pieces == ["满", "两年"]                 # 末块未泄漏成空片段
    assert got == [{"prompt_tokens": 14, "completion_tokens": 1,
                    "total_tokens": 15}]
    assert seen[0]["stream_options"] == {"include_usage": True}
    assert seen[0]["stream"] is True


async def test_stream_without_sink_sends_no_stream_options_via_real_sdk():
    """第 4 点（既有契约零变化）钉在**真实请求体**上：不传 usage_sink 时，
    发出去的 JSON 里完全不出现 stream_options 这个键（不是"值为 None/False"，
    是整键不存在），内容解析路径与改造前一致。"""
    seen: list[dict] = []
    p = _provider_with_real_sdk(seen)

    pieces = [t async for t in p.stream([{"role": "user", "content": "hi"}])]

    assert pieces == ["满", "两年"]
    assert "stream_options" not in seen[0]
    assert seen[0]["stream"] is True
    assert seen[0]["messages"] == [{"role": "user", "content": "hi"}]


async def test_non_param_errors_are_not_retried():
    """非参数类错误（401/429/5xx 等）**不得**触发降级重试——重试等于在限流或
    鉴权失败时白打一次上游，429 时尤其糟。判定只看参数类信号（见
    kbase/plugins/llm/openai_compat._looks_like_param_rejection）。"""
    create = _FakeCreate([_FakeChunk("好")], fail_times=2,
                         exc=RuntimeError("429 rate limit exceeded"))
    llm = _provider_with_fake_create(create)
    sink_calls: list[dict] = []
    with pytest.raises(RuntimeError):
        async for _ in llm.stream([{"role": "user", "content": "hi"}],
                                  usage_sink=sink_calls.append):
            pass
    assert len(create.calls) == 1, (
        f"非参数类错误不该重试，实际打了 {len(create.calls)} 次上游")
    assert create.calls[0]["stream_options"] == {"include_usage": True}
    assert sink_calls == []
