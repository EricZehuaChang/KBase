"""F 满血 VLM 深度识别：视觉 API 调用形状、pending_review 状态机、
人工校验（编辑+确认）后才向量化、按模式重试。"""
import httpx
import pytest
from fastapi.testclient import TestClient

from kbase import vlm_parse
from kbase.api.main import create_app
from kbase.vlm_parse import VLMParseError, parse_image
from tests.test_api import CFG, FakeLLM

PROVIDER = {"name": "glm-v", "base_url": "https://api.v.com/v1",
            "api_key": "sk-v", "api_key_env": "", "model": "glm-5.2v",
            "params": {"extra_body": {"thinking": {"type": "disabled"}}}}


def _png(tmp_path, name="概念图.png"):
    p = tmp_path / name
    p.write_bytes(b"\x89PNG-fake")
    return p


# ---------------- parse_image 单元 ----------------


def test_parse_image_request_shape_and_params(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "# 架构图\n组件A→组件B"}}]})

    out = parse_image(_png(tmp_path), PROVIDER,
                      transport=httpx.MockTransport(handler))
    assert out == "# 架构图\n组件A→组件B"
    assert captured["url"] == "https://api.v.com/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-v"
    body = captured["body"]
    assert body["model"] == "glm-5.2v"
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "转写" in parts[1]["text"]
    assert body["thinking"] == {"type": "disabled"}   # extra_body 平铺进顶层


def test_parse_image_errors(tmp_path):
    with pytest.raises(VLMParseError, match="密钥"):
        parse_image(_png(tmp_path), {**PROVIDER, "api_key": None})

    def err500(request):
        return httpx.Response(500, text="boom")
    with pytest.raises(VLMParseError, match="500"):
        parse_image(_png(tmp_path), PROVIDER,
                    transport=httpx.MockTransport(err500))

    def empty(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": " "}}]})
    with pytest.raises(VLMParseError, match="空内容"):
        parse_image(_png(tmp_path), PROVIDER,
                    transport=httpx.MockTransport(empty))


# ---------------- 端到端：上传→pending_review→编辑确认→可检索 ----------------


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def test_vlm_flow_review_then_index(tmp_path, fake_embedder, monkeypatch):
    monkeypatch.setattr(vlm_parse, "parse_image",
                        lambda path, provider, **kw: "# 部署架构图\n负载均衡器将请求分发到两台应用服务器。")
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents", data={"parse_mode": "vlm"},
           files=[("files", ("架构图.png", b"\x89PNG-fake", "image/png"))])
    doc = c.get(f"/api/kb/{kb_id}/documents").json()[0]
    assert doc["status"] == "pending_review", doc.get("error")

    # 未向量化：检索不应命中，chunks 为空
    assert c.get(f"/api/documents/{doc['id']}/chunks").json()["total"] == 0
    # 识别结果可读（管理员对照原图校验用）
    md = c.get(f"/api/documents/{doc['id']}/content").json()["markdown"]
    assert "负载均衡器" in md

    # 编辑修正后确认入库（VLM 幻觉在此拦截）
    r = c.put(f"/api/documents/{doc['id']}/review",
              json={"markdown": md.replace("两台", "三台高可用")})
    assert r.status_code == 200 and r.json()["status"] == "ready"
    blocks = c.post(f"/api/kb/{kb_id}/search",
                    json={"query": "三台高可用 应用服务器", "top_k": 3}).json()["blocks"]
    assert blocks and "三台高可用" in blocks[0]["snippet"]

    # 重复确认被拒（已 ready）
    assert c.put(f"/api/documents/{doc['id']}/review", json={}).status_code == 409


def test_vlm_failure_marks_failed_and_retry_reruns_vlm(tmp_path, fake_embedder,
                                                       monkeypatch):
    calls = {"n": 0}

    def flaky(path, provider, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise VLMParseError("模拟服务异常")
        return "第二次成功的识别结果"

    monkeypatch.setattr(vlm_parse, "parse_image", flaky)
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents", data={"parse_mode": "vlm"},
           files=[("files", ("图.png", b"\x89PNG-x", "image/png"))])
    doc = c.get(f"/api/kb/{kb_id}/documents").json()[0]
    assert doc["status"] == "failed" and "模拟服务异常" in doc["error"]
    # 重试按落库的 parse_mode 重走 VLM（而不是掉回 auto 管道）
    r = c.post(f"/api/documents/{doc['id']}/retry").json()
    assert r["status"] == "pending_review" and calls["n"] == 2


def test_vlm_mode_non_image_falls_back_to_auto(tmp_path, fake_embedder, monkeypatch):
    """非图片文件选了 vlm：回落既有管道正常入库（宽容降级，不报错）。"""
    monkeypatch.setattr(vlm_parse, "parse_image",
                        lambda *a, **kw: pytest.fail("非图片不该调 VLM"))
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents", data={"parse_mode": "vlm"},
           files=[("files", ("说明.md", "# 标题\n正文内容。".encode(), "text/markdown"))])
    doc = c.get(f"/api/kb/{kb_id}/documents").json()[0]
    assert doc["status"] == "ready"
