"""GLM-OCR（智谱 layout_parsing）适配器单测：MockTransport 全离线验证
请求形状（URL/鉴权/JSON body）与错误面（不可达/非2xx/缺字段→OCRUnavailable）。"""
import base64
import json

import httpx
import pytest

from kbase.plugins.base import OCRUnavailable
from kbase.plugins.ocr.glm_http import GLMOCRBackend


def _write_png(tmp_path):
    """最小可用的假图片文件（内容随意，适配器只做 base64，不解码图片）。"""
    p = tmp_path / "扫描件.png"
    p.write_bytes(b"\x89PNG-fake-bytes")
    return p


def test_success_returns_md_results(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "task-1", "model": "glm-ocr",
            "md_results": "# 发票\n金额：35.42",
            "layout_details": [], "usage": {"total_tokens": 100}})

    backend = GLMOCRBackend(api_key="sk-test-glm",
                            transport=httpx.MockTransport(handler))
    path = _write_png(tmp_path)
    result = backend.to_markdown(path)

    assert result.markdown == "# 发票\n金额：35.42"
    assert result.confidence == 1.0                     # API 无置信度，默认未知
    assert captured["url"] == (
        "https://open.bigmodel.cn/api/paas/v4/layout_parsing")
    assert captured["auth"] == "Bearer sk-test-glm"
    assert captured["body"]["model"] == "glm-ocr"
    # file 字段必须是 data URI（裸 base64 被服务端 1214 拒绝，实测确认），
    # base64 部分能原样解回文件字节
    prefix = "data:image/png;base64,"
    assert captured["body"]["file"].startswith(prefix)
    assert base64.b64decode(captured["body"]["file"][len(prefix):]) == path.read_bytes()


def test_custom_endpoint_and_model(tmp_path):
    """vLLM 本地部署同模型时：endpoint/model 可覆盖（云本同源切档）。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["model"] = json.loads(request.content)["model"]
        return httpx.Response(200, json={"md_results": "内容"})

    backend = GLMOCRBackend(endpoint="http://localhost:8300/v1/",
                            model="glm-ocr-local", api_key="k",
                            transport=httpx.MockTransport(handler))
    backend.to_markdown(_write_png(tmp_path))
    assert captured["url"] == "http://localhost:8300/v1/layout_parsing"
    assert captured["model"] == "glm-ocr-local"


def test_missing_key_raises_unavailable(tmp_path, monkeypatch):
    """密钥缺失→OCRUnavailable（pending_ocr 可重试），且不在构造时抛
    ——密钥问题不该阻塞应用启动。"""
    monkeypatch.delenv("NO_GLM_KEY", raising=False)
    backend = GLMOCRBackend(api_key_env="NO_GLM_KEY")   # 构造不抛
    with pytest.raises(OCRUnavailable, match="NO_GLM_KEY"):
        backend.to_markdown(_write_png(tmp_path))


def test_env_key_used_when_no_direct_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_GLM_KEY", "sk-from-env")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"md_results": "x"})

    backend = GLMOCRBackend(api_key_env="TEST_GLM_KEY",
                            transport=httpx.MockTransport(handler))
    backend.to_markdown(_write_png(tmp_path))
    assert captured["auth"] == "Bearer sk-from-env"


def test_connect_error_raises_unavailable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    backend = GLMOCRBackend(api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(OCRUnavailable, match="不可达"):
        backend.to_markdown(_write_png(tmp_path))


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_error_raises_unavailable(tmp_path, status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "x"})

    backend = GLMOCRBackend(api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(OCRUnavailable, match=str(status)):
        backend.to_markdown(_write_png(tmp_path))


def test_missing_md_results_raises_unavailable(tmp_path):
    """契约防御：响应缺 md_results/为空 → 可重试异常，而不是把空串当解析结果
    （空 markdown 会在 pipeline 里被当成"解析结果为空"判 failed，语义错误）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "t", "md_results": ""})

    backend = GLMOCRBackend(api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(OCRUnavailable, match="md_results"):
        backend.to_markdown(_write_png(tmp_path))


def test_services_wiring_selects_glm_backend(tmp_path, fake_embedder):
    """配置 backend=glm-ocr 时，build_services 应创建 GLMOCRBackend 并注入
    pipeline（不依赖网络：创建后端不发请求，密钥检查延迟到首次调用）。"""
    from kbase.api.services import build_services
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(
        f"data_dir: {str(tmp_path / 'data')!r}\n"
        "ocr: {enabled: true, backend: glm-ocr}\n"
        "retrieval: {hybrid: false, rerank: {enabled: false}}\n"
        "llm:\n  active: fake\n  providers:\n"
        "    - {name: fake, base_url: 'http://x', api_key_env: K, model: m}\n",
        encoding="utf-8")
    svc = build_services(cfg, embedder=fake_embedder, llms={"fake": object()},
                         reranker=False, enricher=False, rewriter=False)
    assert type(svc.pipeline._ocr).__name__ == "GLMOCRBackend"
