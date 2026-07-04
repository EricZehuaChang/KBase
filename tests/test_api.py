from fastapi.testclient import TestClient

from kbase.api.main import create_app

CFG = """
data_dir: {data_dir}
chunker: {{name: structure, chunk_size: 200, chunk_overlap: 0}}
llm:
  active: fake
  providers:
    - {{name: fake, base_url: 'http://x', api_key_env: FAKE_KEY, model: m}}
    - {{name: fake2, base_url: 'http://x', api_key_env: FAKE2_KEY, model: m}}
"""

MD = "# 补贴办法\n## 第一章 申领条件\n连续工作满两年可申领住房补贴。\n"


class FakeLLM:
    model = "fake"

    def __init__(self):
        self.last_messages = None

    async def stream(self, messages, **params):
        self.last_messages = messages
        yield "满两年"
        yield "可申领[1]。"

    async def complete(self, messages, **params):
        return "好"


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False)
    return TestClient(app)


def test_kb_create_and_list(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.post("/api/kb", json={"name": "政策库"})
    assert r.status_code == 200
    kb_id = r.json()["id"]
    assert any(k["id"] == kb_id for k in c.get("/api/kb").json())


def test_spa_deep_link_serves_index(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.get("/kb")
    # web/ 存在于仓库（构建产物），深链接应回退到 index.html
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    r2 = c.get("/api/nonexistent")
    assert r2.status_code == 404          # API 路径不回退


def test_upload_and_document_status(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    r = c.post(f"/api/kb/{kb_id}/documents",
               files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    assert r.status_code == 200
    # TestClient 中 BackgroundTasks 在响应后同步执行完毕
    docs = c.get(f"/api/kb/{kb_id}/documents").json()
    assert docs[0]["filename"] == "补贴办法.md"
    assert docs[0]["status"] == "ready"


def test_query_sse_stream(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    # FakeEmbedder 哈希确定性：用叶子块向量化文本原文当查询保证命中
    q = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    with c.stream("POST", f"/api/kb/{kb_id}/query",
                  json={"question": q}) as r:
        body = "".join(r.iter_text())
    assert "event: citations" in body
    assert "补贴办法.md" in body            # 引用里有文档名
    assert "event: token" in body
    assert "event: done" in body


def test_providers_endpoint(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.get("/api/providers").json()
    assert r["active"] == "fake"
    assert "fake" in r["providers"]


def test_upload_filename_sanitized(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    r = c.post(f"/api/kb/{kb_id}/documents",
               files=[("files", ("/../../evil.md", MD.encode("utf-8"), "text/markdown"))])
    assert r.status_code == 200
    docs = c.get(f"/api/kb/{kb_id}/documents").json()
    assert docs[0]["filename"] == "evil.md"          # 路径成分被剥掉
    # data 目录之外不应出现任何文件：uploads 内文件名不含路径分隔符
    uploads = list((tmp_path / "data" / "uploads").iterdir())
    assert all(p.parent.name == "uploads" for p in uploads)


def test_reranker_load_failure_degrades(tmp_path, fake_embedder, monkeypatch):
    """重排模型加载失败时应降级为不重排，应用照常服务，healthz 标记 degraded。"""
    from kbase.plugins.registry import registry as _registry

    orig_create = _registry.create

    def raiser(kind, name, **kw):
        if kind == "reranker":
            raise RuntimeError("模拟模型加载失败")
        return orig_create(kind, name, **kw)

    monkeypatch.setattr(_registry, "create", raiser)
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()})          # reranker=None 走配置加载路径
    c = TestClient(app)
    assert c.get("/healthz").json()["reranker"] == "degraded"
    assert c.post("/api/kb", json={"name": "x"}).status_code == 200


def test_query_missing_provider_key_returns_503(tmp_path, fake_embedder, monkeypatch):
    # fake2 在配置里存在但不在注入缓存中，且其密钥环境变量未设置：
    # 走真实懒创建路径 → OpenAICompatProvider 抛 RuntimeError → 503
    monkeypatch.delenv("FAKE2_KEY", raising=False)
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    r = c.post(f"/api/kb/{kb_id}/query",
               json={"question": "x", "provider": "fake2"})
    assert r.status_code == 503
    assert "FAKE2_KEY" in r.json()["detail"]


class StatefulFakeOCR:
    """可在测试中途翻转的假 OCR 后端：一开始服务不可达（pending_ocr），
    之后"服务恢复"，重试即可转 ready。"""

    def __init__(self):
        self.up = False

    def to_markdown(self, path):
        from kbase.plugins.base import OCRResult, OCRUnavailable
        if not self.up:
            raise OCRUnavailable("service down")
        return OCRResult(markdown="# 扫描件\n识别出的内容。", confidence=0.9)


def _scanned_pdf_bytes() -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (600, 800), "white").save(buf, "PDF")
    return buf.getvalue()


def test_pending_ocr_then_retry_becomes_ready(tmp_path, fake_embedder):
    ocr = StatefulFakeOCR()
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, ocr_backend=ocr)
    c = TestClient(app)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]

    r = c.post(f"/api/kb/{kb_id}/documents",
               files=[("files", ("scan.pdf", _scanned_pdf_bytes(), "application/pdf"))])
    assert r.status_code == 200
    docs = c.get(f"/api/kb/{kb_id}/documents").json()
    assert docs[0]["status"] == "pending_ocr"
    doc_id = docs[0]["id"]

    ocr.up = True     # OCR 服务恢复
    r = c.post(f"/api/documents/{doc_id}/retry")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"
    docs = c.get(f"/api/kb/{kb_id}/documents").json()
    assert docs[0]["status"] == "ready"


def test_retry_ocr_batch_endpoint(tmp_path, fake_embedder):
    ocr = StatefulFakeOCR()
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, ocr_backend=ocr)
    c = TestClient(app)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("scan.pdf", _scanned_pdf_bytes(), "application/pdf"))])
    assert c.get(f"/api/kb/{kb_id}/documents").json()[0]["status"] == "pending_ocr"

    ocr.up = True
    r = c.post(f"/api/kb/{kb_id}/retry-ocr")
    assert r.status_code == 200
    assert len(r.json()["retrying"]) == 1
    # TestClient 中 BackgroundTasks 在响应后同步执行完毕
    assert c.get(f"/api/kb/{kb_id}/documents").json()[0]["status"] == "ready"


def test_kb_config_put_get_and_validation(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]

    # 新建时未设置过 config，GET 列表里该字段为 None
    listed = c.get("/api/kb").json()
    assert next(k for k in listed if k["id"] == kb_id)["config"] is None

    body = {"chunk_size": 500, "chunk_overlap": 50, "enrich": {"enabled": True}}
    r = c.put(f"/api/kb/{kb_id}/config", json=body)
    assert r.status_code == 200

    listed = c.get("/api/kb").json()
    assert next(k for k in listed if k["id"] == kb_id)["config"] == body

    # 非法值 422：chunk_size 超范围
    assert c.put(f"/api/kb/{kb_id}/config",
                 json={"chunk_size": 32}).status_code == 422
    # chunk_overlap 必须 < chunk_size
    assert c.put(f"/api/kb/{kb_id}/config",
                 json={"chunk_size": 100, "chunk_overlap": 100}).status_code == 422
    # enrich.enabled 非 bool
    assert c.put(f"/api/kb/{kb_id}/config",
                 json={"enrich": {"enabled": "yes"}}).status_code == 422
    # 未知 key
    assert c.put(f"/api/kb/{kb_id}/config",
                 json={"unknown_key": 1}).status_code == 422
    # 未知 kb
    assert c.put("/api/kb/not-exist/config",
                 json={"chunk_size": 300}).status_code == 404
