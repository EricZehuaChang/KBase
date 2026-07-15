"""URL 连接器（M6-7）：网页导入走 markitdown 管道可检索、scheme 校验、
拉取失败 502、库不存在 404。httpx 层打桩，不出真网络。"""
import pytest
from fastapi.testclient import TestClient

import kbase.api.routes.kb as kb_routes
from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM

HTML = """<!DOCTYPE html><html><head><title>内网Wiki</title></head><body>
<h1>报销制度</h1><p>差旅住宿费凭发票实报实销，上限每晚500元。</p>
</body></html>"""


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def test_import_url_ingests_page(tmp_path, fake_embedder, monkeypatch):
    monkeypatch.setattr(kb_routes, "_fetch_url",
                        lambda url: (HTML.encode("utf-8"), "wiki-报销制度.html"))
    c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]

    r = c.post(f"/api/kb/{kb}/import-url", json={"url": "http://wiki.corp/报销"})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == ["wiki-报销制度.html"]

    docs = c.get(f"/api/kb/{kb}/documents").json()
    assert len(docs) == 1 and docs[0]["status"] == "ready"

    hits = c.post(f"/api/kb/{kb}/search",
                  json={"query": "住宿费上限", "top_k": 5}).json()["blocks"]
    assert any("500" in b["text"] for b in hits)


def test_import_url_guards(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    # 非 http/https 直接 422（不发请求）
    assert c.post(f"/api/kb/{kb}/import-url",
                  json={"url": "file:///etc/passwd"}).status_code == 422
    # 库不存在 404
    assert c.post("/api/kb/nope/import-url",
                  json={"url": "http://a.b/c"}).status_code == 404


def test_import_url_fetch_error_502(tmp_path, fake_embedder, monkeypatch):
    import httpx

    def boom(url, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", boom)
    c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    r = c.post(f"/api/kb/{kb}/import-url", json={"url": "http://down.host/x"})
    assert r.status_code == 502
