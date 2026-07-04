import asyncio
import os
from typing import AsyncIterator

from kbase.plugins.registry import registry


@registry.register("llm", "openai-compat")
class OpenAICompatProvider:
    """一个实现通吃所有 OpenAI 兼容端点（DashScope/硅基流动/vLLM/DeepSeek）。"""

    def __init__(self, base_url: str, api_key_env: str, model: str,
                 max_concurrency: int = 4):
        from openai import AsyncOpenAI
        key = os.environ.get(api_key_env)
        if not key:
            raise RuntimeError(
                f"环境变量 {api_key_env} 未设置，无法初始化 LLM provider")
        self._client = AsyncOpenAI(base_url=base_url, api_key=key)
        self.model = model
        self._sem = asyncio.Semaphore(max_concurrency)

    async def stream(self, messages: list[dict], **params) -> AsyncIterator[str]:
        async with self._sem:
            resp = await self._client.chat.completions.create(
                model=self.model, messages=messages, stream=True, **params)
            async for chunk in resp:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

    async def complete(self, messages: list[dict], **params) -> str:
        async with self._sem:
            resp = await self._client.chat.completions.create(
                model=self.model, messages=messages, stream=False, **params)
            return resp.choices[0].message.content or ""
