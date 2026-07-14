"""KB 级向量模型选择（M5-2）：openai-embed 适配器、EmbedderPool、
建库绑定与摄取/查询按库解析的端到端行为。"""
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from kbase.plugins.embedders.factory import EmbedderPool
from kbase.plugins.embedders.openai_compat import OpenAICompatEmbedder
from tests.test_api import MD, FakeLLM

# ---------------- openai-embed 适配器 ----------------


def _mock_embed_transport(captured: dict, dim: int = 4):
    """按 OpenAI /embeddings 契约应答；故意把 data 倒序返回，验证按 index 回排。"""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.setdefault("requests", []).append(json.loads(request.content))
        captured["auth"] = request.headers.get("authorization")
        captured["url"] = str(request.url)
        inputs = json.loads(request.content)["input"]
        data = [{"index": i, "embedding": [float(i)] * dim}
                for i in range(len(inputs))]
        return httpx.Response(200, json={"data": list(reversed(data))})

    return httpx.MockTransport(handler)


def test_openai_embed_batches_and_reorders():
    captured: dict = {}
    e = OpenAICompatEmbedder(base_url="https://api.example.com/v1/", model="emb-1",
                             api_key="sk-e", batch_size=2,
                             transport=_mock_embed_transport(captured))
    vecs = e.embed(["a", "b", "c", "d", "e"])
    # 5 条文本按 batch_size=2 分 3 批
    assert len(captured["requests"]) == 3
    assert captured["requests"][0]["input"] == ["a", "b"]
    assert captured["requests"][0]["model"] == "emb-1"
    assert captured["auth"] == "Bearer sk-e"
    assert captured["url"] == "https://api.example.com/v1/embeddings"
    # 服务端 data 倒序返回，客户端必须按 index 回排（首条向量应对应 index 0）
    assert len(vecs) == 5 and vecs[0] == [0.0] * 4 and vecs[1] == [1.0] * 4


def test_openai_embed_dimension_lazy_probe():
    e = OpenAICompatEmbedder(base_url="https://api.example.com/v1", model="m",
                             api_key="k", transport=_mock_embed_transport({}, dim=7))
    assert e.dimension == 7


def test_openai_embed_requires_key(monkeypatch):
    monkeypatch.delenv("NO_EMB_KEY", raising=False)
    with pytest.raises(RuntimeError, match="未配置密钥"):
        OpenAICompatEmbedder(base_url="https://x/v1", model="m",
                             api_key_env="NO_EMB_KEY")


def test_openai_embed_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    e = OpenAICompatEmbedder(base_url="https://x/v1", model="m", api_key="k",
                             transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="500"):
        e.embed(["a"])


# ---------------- EmbedderPool ----------------


def _cfg_with_options(tmp_path, extra_yaml: str = ""):
    from kbase.config import load_config
    p = tmp_path / "kbase.yaml"
    p.write_text(
        f"data_dir: {str(tmp_path / 'data')!r}\n"
        "embedders:\n"
        "  - {id: alt, plugin: bge-local, model: some/alt-model}\n"
        + extra_yaml +
        "llm:\n  active: fake\n  providers:\n"
        "    - {name: fake, base_url: 'http://x', api_key_env: K, model: m}\n",
        encoding="utf-8")
    return load_config(p)


def test_pool_default_injection_and_unknown_id(tmp_path, fake_embedder):
    pool = EmbedderPool(_cfg_with_options(tmp_path), default_embedder=fake_embedder)
    assert pool.get(None) is fake_embedder
    assert pool.get("default") is fake_embedder
    with pytest.raises(KeyError, match="no-such"):
        pool.get("no-such")
    assert pool.known_ids() == {"default", "alt"}
    cat = pool.catalog()
    assert cat["default"]["id"] == "default"
    assert cat["options"] == [{"id": "alt", "plugin": "bge-local",
                               "model": "some/alt-model"}]


def test_pool_lazy_build_and_cache(tmp_path, fake_embedder, monkeypatch):
    from kbase.plugins.registry import registry as _registry
    pool = EmbedderPool(_cfg_with_options(tmp_path), default_embedder=fake_embedder)
    built = []
    orig = _registry.create

    def spy(kind, name, **kw):
        if kind == "embedder":
            built.append((name, kw))
            return fake_embedder
        return orig(kind, name, **kw)

    monkeypatch.setattr(_registry, "create", spy)
    a1 = pool.get("alt")
    a2 = pool.get("alt")
    assert a1 is a2 and len(built) == 1          # 惰性构建且单例缓存
    assert built[0] == ("bge-local", {"model": "some/alt-model"})


def test_config_rejects_duplicate_and_reserved_ids(tmp_path):
    with pytest.raises(Exception, match="重复"):
        _cfg_with_options(tmp_path,
                          "  - {id: alt, plugin: bge-local}\n")
    with pytest.raises(Exception, match="default"):
        _cfg_with_options(tmp_path,
                          "  - {id: default, plugin: bge-local}\n")


# ---------------- API 端到端：建库绑定 → 摄取/查询按库解析 ----------------


class RecordingEmbedder:
    """可观测的假 embedder：记录所有 embed 调用文本，向量确定性生成。"""
    dimension = 8

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            h = int(hashlib.md5(t.encode("utf-8")).hexdigest(), 16)
            out.append([((h >> (i * 4)) % 100) / 100.0 for i in range(8)])
        return out


CFG_WITH_OPTIONS = """
data_dir: {data_dir}
chunker: {{name: structure, chunk_size: 200, chunk_overlap: 0}}
embedders:
  - {{id: alt, plugin: bge-local, model: some/alt-model}}
llm:
  active: fake
  providers:
    - {{name: fake, base_url: 'http://x', api_key_env: FAKE_KEY, model: m}}
"""


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(
        CFG_WITH_OPTIONS.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
        encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def test_embedders_catalog_endpoint(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    got = c.get("/api/embedders").json()
    assert got["default"]["id"] == "default"
    assert [o["id"] for o in got["options"]] == ["alt"]


def test_create_kb_with_unknown_embedder_rejected(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.post("/api/kb", json={"name": "库", "embedder": "nope"})
    assert r.status_code == 422
    assert "nope" in r.json()["detail"]


def test_kb_bound_embedder_used_for_ingest_and_query(tmp_path, fake_embedder,
                                                     monkeypatch):
    """绑定 alt 的库：摄取与检索的向量化都必须走 alt 模型（而不是默认）；
    默认库不受影响。alt 实例通过 monkeypatch registry.create 注入可观测 fake
    ——池是惰性构建，补丁在 app 启动后打上即可拦截首次构建。"""
    c = _client(tmp_path, fake_embedder)
    from kbase.plugins.registry import registry as _registry
    alt = RecordingEmbedder()
    orig = _registry.create

    def fake_create(kind, name, **kw):
        if kind == "embedder" and name == "bge-local":
            return alt
        return orig(kind, name, **kw)

    monkeypatch.setattr(_registry, "create", fake_create)

    r = c.post("/api/kb", json={"name": "绑定库", "embedder": "alt"})
    assert r.json()["embedder"] == "alt"
    kb_id = r.json()["id"]
    # KB 列表的 config 里能看到绑定（前端展示用）
    kb_row = next(k for k in c.get("/api/kb").json() if k["id"] == kb_id)
    assert kb_row["config"]["embedder"] == "alt"

    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    docs = c.get(f"/api/kb/{kb_id}/documents").json()
    assert docs[0]["status"] == "ready"
    assert len(alt.calls) == 1                     # 摄取向量化走了 alt

    c.post(f"/api/kb/{kb_id}/search", json={"query": "住房补贴", "top_k": 3})
    assert len(alt.calls) == 2                     # 查询向量化也走 alt
    assert alt.calls[-1] == ["住房补贴"]

    # 默认库走注入的默认 embedder，不触碰 alt
    kb2 = c.post("/api/kb", json={"name": "默认库"}).json()["id"]
    c.post(f"/api/kb/{kb2}/documents",
           files=[("files", ("b.md", (MD + "x").encode("utf-8"), "text/markdown"))])
    assert c.get(f"/api/kb/{kb2}/documents").json()[0]["status"] == "ready"
    assert len(alt.calls) == 2                     # alt 调用数不变


def test_kb_config_put_preserves_embedder_binding(tmp_path, fake_embedder):
    """调分块参数不得冲掉 embedder 绑定（否则该库静默回落默认模型）。"""
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库", "embedder": "alt"}).json()["id"]
    assert c.put(f"/api/kb/{kb_id}/config",
                 json={"chunk_size": 256}).status_code == 200
    kb_row = next(k for k in c.get("/api/kb").json() if k["id"] == kb_id)
    assert kb_row["config"]["chunk_size"] == 256
    assert kb_row["config"]["embedder"] == "alt"
