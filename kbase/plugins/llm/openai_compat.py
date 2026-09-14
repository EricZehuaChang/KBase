import asyncio
import os
from typing import AsyncIterator

from kbase.plugins.registry import registry


@registry.register("llm", "openai-compat")
class OpenAICompatProvider:
    """一个实现通吃所有 OpenAI 兼容端点（DashScope/硅基流动/vLLM/DeepSeek）。"""

    def __init__(self, base_url: str, api_key_env: str = "", model: str = "",
                 max_concurrency: int = 4, params: dict | None = None,
                 api_key: str | None = None):
        from openai import AsyncOpenAI
        # 密钥解析顺序（M5-2）：页面直配的 api_key（DB 存储）优先，
        # 其次 api_key_env 指向的环境变量——两者都没有才报错。
        key = api_key or (os.environ.get(api_key_env) if api_key_env else None)
        if not key:
            raise RuntimeError(
                f"provider 未配置密钥：api_key 为空且环境变量 "
                f"{api_key_env or '(未指定)'} 未设置，无法初始化 LLM provider")
        self._client = AsyncOpenAI(base_url=base_url, api_key=key)
        self.model = model
        self._sem = asyncio.Semaphore(max_concurrency)
        # provider 级默认参数（如 extra_body 关闭 qwen3 thinking），调用点参数优先
        self._default_params = params or {}

    async def stream(self, messages: list[dict], *, usage_sink=None,
                     **params) -> AsyncIterator[str]:
        """流式生成。usage_sink（可选）：拿到上游真实 usage 时回调一次
        （dict(prompt_tokens/completion_tokens/total_tokens)）。

        T09 计量：不再往下游"猜"token 数——OpenAI 兼容端点支持
        stream_options={"include_usage": True} 时，末块会带 usage（末端
        chunk 的 choices 为空）。该参数**只在调用方明确要用量时才加**：
        不是所有兼容端点都接受它，无条件发等于拿流式可用性换计量精度；
        真遇到不接受的端点，捕获异常后去掉参数重试一次（降级为"无真实
        用量"，调用方的 usage_sink 拿不到值，由上层退回估算），而不是让
        整个问答失败。

        实现细节：`async with self._sem` 覆盖整个迭代过程，所以每个
        provider 实例同时只有一个流在跑（max_concurrency 是实例级信号量）
        ——usage_sink 回调因此不会串台。
        """
        merged = {**self._default_params, **params}
        if usage_sink is not None:
            # 显式传入优先（provider params 里若已配 stream_options，以调用方为准）
            merged["stream_options"] = {"include_usage": True}
        async with self._sem:
            try:
                resp = await self._client.chat.completions.create(
                    model=self.model, messages=messages, stream=True, **merged)
            except Exception as exc:      # noqa: BLE001 需按类型判定是否降级
                if usage_sink is None or "stream_options" not in merged:
                    raise
                if not _looks_like_param_rejection(exc):
                    # 401/403/429/5xx 等**不是参数问题**：重试只会白打一次上游
                    # （429 时尤其糟——等于在限流时立刻再来一发）。直接抛，
                    # 调用方按原有错误路径处理。
                    raise
                # 端点不接受 stream_options（多为 400 invalid_request_error）：
                # 去掉它重试，拿到的是"没有真实用量"的正常流，不是失败。
                import logging
                logging.getLogger(__name__).warning(
                    "端点不接受 stream_options.include_usage，已降级为无用量流",
                    exc_info=True)
                merged.pop("stream_options", None)
                resp = await self._client.chat.completions.create(
                    model=self.model, messages=messages, stream=True, **merged)
            async for chunk in resp:
                # 末块的 usage 先于内容判断处理：带 usage 的 chunk 的 choices
                # 通常为空，绝不能因为它没有文本就把 usage 一起丢掉
                if usage_sink is not None and getattr(chunk, "usage", None):
                    u = chunk.usage
                    usage_sink({"prompt_tokens": u.prompt_tokens or 0,
                                "completion_tokens": u.completion_tokens or 0,
                                "total_tokens": u.total_tokens or 0})
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

    async def complete(self, messages: list[dict], *, usage_sink=None,
                       **params) -> str:
        """非流式生成。usage_sink 语义与 stream() 相同（T09 计量用）。"""
        merged = {**self._default_params, **params}
        async with self._sem:
            resp = await self._client.chat.completions.create(
                model=self.model, messages=messages, stream=False, **merged)
            if usage_sink is not None and getattr(resp, "usage", None):
                u = resp.usage
                usage_sink({"prompt_tokens": u.prompt_tokens or 0,
                            "completion_tokens": u.completion_tokens or 0,
                            "total_tokens": u.total_tokens or 0})
            return resp.choices[0].message.content or ""

def _looks_like_param_rejection(exc: BaseException) -> bool:
    """判断异常是否属于"端点不接受某个参数"——只有这类才值得去掉参数重试。

    取三种信号，任一命中即认定（宁可漏降级也不要误重试）：
    - openai 的 BadRequestError（HTTP 400，参数类错误的载体）；
    - 异常文本里出现 stream_options / include_usage（端点明说了是它）；
    - TypeError（SDK/端点签名不接受该关键字，属本地即知类）。
    401/403/429/5xx 与网络错误都不匹配，因此不会被重试。
    """
    try:
        from openai import BadRequestError
        if isinstance(exc, BadRequestError):
            return True
    except Exception:              # noqa: BLE001 openai 缺失/版本差异时不阻塞
        pass
    if isinstance(exc, TypeError):
        return True
    text = str(exc).lower()
    return "stream_options" in text or "include_usage" in text
