"""GLM-OCR（智谱云 layout_parsing）HTTP 适配器。

真实 API 契约（docs.bigmodel.cn「文档解析」页，2026-07 查证）：
- POST {base}/layout_parsing，Content-Type: application/json，
  Authorization: Bearer <api_key>
- 请求体：{"model": "glm-ocr", "file": "<文件 URL 或 base64>"}；本适配器处理
  本地已落盘的上传文件，走 base64 分支。**实测注意**：文档只说"base64 编码"，
  但裸 base64 会被 400 拒绝（错误码 1214"OCR仅支持PDF、JPG、PNG、JPEG格式"
  ——报错文案是格式不支持，真实原因是服务端靠 data URI 前缀识别格式），
  必须发 "data:{mime};base64,..." 形式（2026-07-15 真机实测确认）。
  支持图片（≤10MB）与 PDF（≤50MB、≤100页），与摄取管道的 OCR 路由范围
  （_IMAGE_EXTS + 无文本层 PDF，见 ingest/pipeline.py）一致。
- 响应体：md_results（str，整份文档的 Markdown 转写）为本适配器唯一消费的
  字段；layout_details（bbox_2d/label/content 版式明细）与 usage 不消费。
  markdown 直接在响应体里，无需二次下载（对比 MonkeyOCR 的 zip 链路）。
- API 不返回置信度，OCRResult.confidence 沿用默认 1.0（与 MonkeyOCR 同语义：
  1.0=未知，质量门控不得当作高置信）。

错误语义与 monkey_http 保持一致：网络不可达/超时/非 2xx/响应缺 md_results
一律抛 OCRUnavailable——文档转 pending_ocr 可重试，而不是永久 failed。
密钥未配置也抛 OCRUnavailable（在首次调用时才检查，不在构造时）：
密钥缺失是部署配置问题，不应阻塞应用启动，修好后走批量重试
（POST /api/kb/{id}/retry-ocr）即可恢复存量文档。
"""
import base64
import os
from pathlib import Path

import httpx

from kbase.plugins.base import OCRResult, OCRUnavailable
from kbase.plugins.registry import registry

_DEFAULT_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4"

# data URI 的 MIME 映射：覆盖摄取管道会路由到 OCR 的全部后缀
# （_IMAGE_EXTS + .pdf，见 ingest/pipeline.py）。未知后缀兜底 octet-stream
# ——服务端会以 1214 拒绝，错误如实透出为 OCRUnavailable。
_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


@registry.register("ocr", "glm-ocr")
class GLMOCRBackend:
    def __init__(self, endpoint: str = "", api_key_env: str = "ZHIPU_API_KEY",
                 model: str = "glm-ocr", timeout: float = 300.0,
                 api_key: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        # endpoint 留空走智谱官方云；standard/air-gapped 档用 vLLM 本地起
        # 同一模型、暴露相同 API 形状时，把 endpoint 指到本地服务即可切换
        # （云本同源，见 project05 选型结论）。
        self._endpoint = (endpoint or _DEFAULT_ENDPOINT).rstrip("/")
        self._api_key_env = api_key_env
        self._api_key = api_key
        self._model = model
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def _resolve_key(self) -> str:
        # 直传 key 优先，其次环境变量——与 openai_compat 的解析顺序一致。
        key = self._api_key or os.environ.get(self._api_key_env)
        if not key:
            raise OCRUnavailable(
                f"GLM-OCR 未配置密钥：环境变量 {self._api_key_env} 未设置")
        return key

    def to_markdown(self, path) -> OCRResult:
        path = Path(path)
        key = self._resolve_key()
        mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")
        # 必须是 data URI：裸 base64 被服务端 400/1214 拒绝（见模块顶部实测注记）
        file_uri = (f"data:{mime};base64,"
                    + base64.b64encode(path.read_bytes()).decode("ascii"))
        try:
            resp = self._client.post(
                f"{self._endpoint}/layout_parsing",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": self._model, "file": file_uri})
            resp.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise OCRUnavailable(f"GLM-OCR 服务不可达: {e}") from e
        except httpx.HTTPStatusError as e:
            raise OCRUnavailable(
                f"GLM-OCR 服务错误: {e.response.status_code}") from e
        data = resp.json()
        markdown = data.get("md_results")
        if not markdown or not str(markdown).strip():
            # 契约防御：正常响应必含非空 md_results；缺失视为服务侧异常，
            # 按可重试处理（而不是把文档判 failed）。
            raise OCRUnavailable("GLM-OCR 响应缺少 md_results，无法取得解析结果")
        # layout_details：逐页 bbox/label/content 版式明细（M6 表格版存档）。
        # 表格语义已随 md_results 的 Markdown 表格进表格感知分块，这份明细
        # 供 bbox 引用高亮等后续能力使用，缺失不影响任何现有链路。
        return OCRResult(markdown=str(markdown),
                         layout=data.get("layout_details") or None)
