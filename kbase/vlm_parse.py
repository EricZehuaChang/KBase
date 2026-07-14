"""满血 VLM 深度识别（F）：把复杂图（概念图/时序图/架构图/PPT 截图）交给
在线强力视觉大模型转写为结构化 Markdown。

与 GLM-OCR 的分工：GLM-OCR 是"忠实转写"（文字/表格照抄，快、便宜、不推
理）；本模块是"理解性转写"（时序图→交互步骤、架构图→组件关系描述），
用满血多模态模型，贵而慢——所以是**上传时用户显式选择**的模式，且结果
必须经人工校验确认后才向量化（防幻觉进知识库，见 pipeline pending_review）。

实现为同步 httpx 直调 OpenAI 兼容 /chat/completions（视觉消息 image_url
data URI）——摄取管道跑在线程池同步代码里，不引 async 依赖；provider 的
base_url/key/model 来自 providers_store（页面可配的同一套体系）。
"""
import base64
import mimetypes
import os
from pathlib import Path

import httpx

_PROMPT = (
    "你是文档资料转写专家。请把这张图片的全部信息转写为结构化 Markdown，"
    "供知识库检索使用：\n"
    "- 文字与数据逐字保留，表格转为 Markdown 表格；\n"
    "- 流程图/时序图转为有序步骤或交互序列（谁→谁：做什么）；\n"
    "- 架构图/概念图描述组件与关系；\n"
    "- 只转写图中实际存在的内容，不确定处标注[不确定]，严禁编造。\n"
    "直接输出 Markdown，不要任何前言或解释。"
)


class VLMParseError(RuntimeError):
    """VLM 识别失败（网络/鉴权/空返回）。上传链路把它映射为文档 failed
    并附原因——与 OCRUnavailable 不同，这不是'稍后重试就好'的暂态语义，
    但文档行的「重试」按钮仍可再走一次。"""


def parse_image(path, provider: dict, timeout: float = 300.0,
                transport: httpx.BaseTransport | None = None) -> str:
    """图片 → Markdown。provider 为 providers_store.get_provider_dict 的
    完整字典（含 api_key/api_key_env/base_url/model）。"""
    p = Path(path)
    key = provider.get("api_key") or (
        os.environ.get(provider["api_key_env"]) if provider.get("api_key_env") else None)
    if not key:
        raise VLMParseError(
            f"VLM provider {provider.get('name', '?')} 未配置可用密钥")
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    data_uri = f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
    body = {
        "model": provider["model"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": _PROMPT},
            ],
        }],
        # 复用 provider 的默认参数（如关思考模式的 extra_body 内容需平铺——
        # openai SDK 的 extra_body 是客户端概念，直调 REST 时并进顶层即可）
        **(provider.get("params", {}).get("extra_body") or {}),
    }
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.post(
                f"{provider['base_url'].rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {key}"}, json=body)
            resp.raise_for_status()
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        raise VLMParseError(f"VLM 服务不可达（{provider['base_url']}）: {e}") from e
    except httpx.HTTPStatusError as e:
        raise VLMParseError(
            f"VLM 服务返回错误（{provider['base_url']}）: "
            f"{e.response.status_code} {e.response.text[:120]}") from e
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise VLMParseError("VLM 响应缺少 choices[0].message.content") from e
    if not content or not content.strip():
        raise VLMParseError("VLM 返回了空内容")
    return content.strip()
