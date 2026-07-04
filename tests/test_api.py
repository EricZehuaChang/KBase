from fastapi.testclient import TestClient

from kbase.api.main import create_app

CFG = """
data_dir: {data_dir}
chunker: {{name: structure, chunk_size: 200, chunk_overlap: 0}}
llm:
  active: fake
  providers:
    - {{name: fake, base_url: 'http://x', api_key_env: FAKE_KEY, model: m}}
"""

MD = "# 补贴办法\n## 第一章 申领条件\n连续工作满两年可申领住房补贴。\n"


class FakeLLM:
    model = "fake"

    async def stream(self, messages, **params):
        yield "满两年"
        yield "可申领[1]。"


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()})
    return TestClient(app)


def test_kb_create_and_list(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.post("/api/kb", json={"name": "政策库"})
    assert r.status_code == 200
    kb_id = r.json()["id"]
    assert any(k["id"] == kb_id for k in c.get("/api/kb").json())


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
