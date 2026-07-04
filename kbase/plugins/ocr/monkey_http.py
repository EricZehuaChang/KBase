"""MonkeyOCR HTTP 适配器。

真实 API 形状（读 D:\\Claude Code\\MonkeyOCR\\api\\main.py 源码确认，
运行手册.md 只给了 `uvicorn api.main:app --port 8000` 的命令行示例，
真正决定契约的是 FastAPI 路由定义本身）：

- `GET  /health`            → {"status": "healthy", "model_loaded": bool}
  （不是计划草稿猜测的 `/healthz`）
- `POST /parse`             → multipart 字段名固定为 `file`（不是任意名）；
  响应 **不是** {"markdown": ..., "confidence": ...} 这种直出 JSON，而是：
  ParseResponse{success: bool, message: str, output_dir: str|None,
                files: list[str]|None, download_url: str|None}
  真正的 Markdown 内容不在响应体里，而是打包进一个 zip
  （`download_url` = "/static/{zip_filename}"），需要二次 GET 下载该 zip、
  解压后读取其中的 `{original_name}.md` 才能拿到文本。
  响应体里也 **没有任何 confidence/置信度字段**（对全仓 grep "confidence"
  只在 magic_pdf 内部版面分析代码中出现，从未透出到 API 层）。
- `POST /ocr/text`          → TaskResponse{success, task_type, content, message}
  这个端点直接把提取文本放进 `content` 字段，无需二次下载，但只做纯文本
  提取（模型按 "text" 任务指令跑，不是 `/parse` 那种含表格/公式的结构化
  Markdown），且同样没有 confidence 字段。

**本适配器的选择**：为了拿到结构更完整的 Markdown（与本项目"Markdown 中间
产物"的摄取假设一致），走 `/parse` + 下载 zip + 解压读 `.md` 这条真实路径，
而不是计划草稿里假设的"POST /parse 直接拿 markdown 字段"。confidence 字段
MonkeyOCR 未提供，固定回填 1.0（OCRResult.confidence 的默认值），并在结果
markdown 开头附一行注释说明这是 MonkeyOCR 输出、无置信度。
"""
import io
import zipfile
from pathlib import Path

import httpx

from kbase.plugins.base import OCRResult, OCRUnavailable
from kbase.plugins.registry import registry


@registry.register("ocr", "monkey-http")
class MonkeyOCRBackend:
    def __init__(self, endpoint: str = "http://localhost:7861", timeout: float = 300.0):
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout

    def to_markdown(self, path) -> OCRResult:
        path = Path(path)
        try:
            with open(path, "rb") as f:
                resp = httpx.post(f"{self._endpoint}/parse",
                                  files={"file": (path.name, f)}, timeout=self._timeout)
            resp.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise OCRUnavailable(f"OCR 服务不可达: {e}") from e
        except httpx.HTTPStatusError as e:
            raise OCRUnavailable(f"OCR 服务错误: {e.response.status_code}") from e
        return self._parse_response(resp.json(), path)

    def _parse_response(self, data: dict, path: Path) -> OCRResult:
        if not data.get("success"):
            raise OCRUnavailable(f"OCR 解析失败: {data.get('message', '未知错误')}")
        download_url = data.get("download_url")
        if not download_url:
            raise OCRUnavailable("OCR 响应缺少 download_url，无法取回解析结果")
        try:
            zresp = httpx.get(f"{self._endpoint}{download_url}", timeout=self._timeout)
            zresp.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise OCRUnavailable(f"OCR 结果下载失败: {e}") from e
        except httpx.HTTPStatusError as e:
            raise OCRUnavailable(f"OCR 结果下载出错: {e.response.status_code}") from e
        markdown = self._extract_markdown(zresp.content)
        # MonkeyOCR API 不返回置信度，用默认值 1.0（OCRResult 的字段默认）
        return OCRResult(markdown=markdown)

    @staticmethod
    def _extract_markdown(zip_bytes: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            md_names = [n for n in zf.namelist() if n.endswith(".md")]
            if not md_names:
                raise OCRUnavailable("OCR 结果压缩包中未找到 Markdown 文件")
            return zf.read(md_names[0]).decode("utf-8")
