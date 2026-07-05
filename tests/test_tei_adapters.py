"""TEI（text-embeddings-inference）HTTP 适配器：embedder 与 reranker。

用 httpx.MockTransport 假 TEI 服务，不需要真实容器/网络。
"""
import httpx
import pytest


# ---------------------------------------------------------------------------
# TEIEmbedder
# ---------------------------------------------------------------------------

def test_tei_embedder_embed_happy_path():
    from kbase.plugins.embedders.tei import TEIEmbedder

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        assert request.url.path == "/embed"
        payload = json.loads(request.content)
        texts = payload["inputs"]
        return httpx.Response(200, json=[[float(len(t)), 0.0] for t in texts])

    transport = httpx.MockTransport(handler)
    e = TEIEmbedder("http://tei-embed:80", transport=transport)
    vecs = e.embed(["ab", "abcd"])
    assert vecs == [[2.0, 0.0], [4.0, 0.0]]


def test_tei_embedder_dimension_probes_once():
    from kbase.plugins.embedders.tei import TEIEmbedder

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        payload = json.loads(request.content)
        calls["n"] += 1
        return httpx.Response(200, json=[[1.0, 2.0, 3.0] for _ in payload["inputs"]])

    transport = httpx.MockTransport(handler)
    e = TEIEmbedder("http://tei-embed:80", transport=transport)
    assert calls["n"] == 0                 # 惰性：构造时不探测
    assert e.dimension == 3
    assert calls["n"] == 1
    assert e.dimension == 3                # 再次访问命中缓存，不重复探测
    assert calls["n"] == 1


def test_tei_embedder_chunks_large_batches():
    from kbase.plugins.embedders.tei import TEIEmbedder

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        payload = json.loads(request.content)
        calls.append(len(payload["inputs"]))
        return httpx.Response(200, json=[[1.0] for _ in payload["inputs"]])

    transport = httpx.MockTransport(handler)
    e = TEIEmbedder("http://tei-embed:80", transport=transport)
    texts = [f"t{i}" for i in range(100)]
    vecs = e.embed(texts)
    assert len(vecs) == 100
    assert calls == [64, 36]                # 100 texts -> 两批：64 + 36


def test_tei_embedder_connect_error_raises_runtime_error():
    from kbase.plugins.embedders.tei import TEIEmbedder

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    transport = httpx.MockTransport(handler)
    e = TEIEmbedder("http://tei-embed:80", transport=transport)
    with pytest.raises(RuntimeError) as exc_info:
        e.embed(["x"])
    assert "http://tei-embed:80" in str(exc_info.value)


def test_tei_embedder_timeout_raises_runtime_error():
    from kbase.plugins.embedders.tei import TEIEmbedder

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    transport = httpx.MockTransport(handler)
    e = TEIEmbedder("http://tei-embed:80", transport=transport)
    with pytest.raises(RuntimeError) as exc_info:
        e.embed(["x"])
    assert "http://tei-embed:80" in str(exc_info.value)


def test_tei_embedder_http_error_raises_runtime_error():
    from kbase.plugins.embedders.tei import TEIEmbedder

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    transport = httpx.MockTransport(handler)
    e = TEIEmbedder("http://tei-embed:80", transport=transport)
    with pytest.raises(RuntimeError) as exc_info:
        e.embed(["x"])
    assert "http://tei-embed:80" in str(exc_info.value)


def test_tei_embedder_registered_in_registry():
    import kbase.plugins.embedders.tei  # noqa: F401
    from kbase.plugins.registry import registry

    e = registry.create("embedder", "tei", endpoint="http://tei-embed:80",
                        transport=httpx.MockTransport(
                            lambda r: httpx.Response(200, json=[[1.0]])))
    assert e is not None


# ---------------------------------------------------------------------------
# TEIReranker
# ---------------------------------------------------------------------------

def test_tei_reranker_happy_path_out_of_order_response():
    from kbase.plugins.rerankers.tei import TEIReranker

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rerank"
        # TEI 常按分数降序返回，故意乱序：index 2 排第一
        return httpx.Response(200, json=[
            {"index": 2, "score": 0.9},
            {"index": 0, "score": 0.5},
            {"index": 1, "score": 0.1},
        ])

    transport = httpx.MockTransport(handler)
    r = TEIReranker("http://tei-rerank:80", transport=transport)
    scores = r.rerank("query", ["t0", "t1", "t2"])
    assert scores == [0.5, 0.1, 0.9]          # 按原始文本顺序返回分数


def test_tei_reranker_empty_texts_no_http_call():
    from kbase.plugins.rerankers.tei import TEIReranker

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不应发起 HTTP 请求")

    transport = httpx.MockTransport(handler)
    r = TEIReranker("http://tei-rerank:80", transport=transport)
    assert r.rerank("query", []) == []


def test_tei_reranker_http_error_raises_runtime_error():
    from kbase.plugins.rerankers.tei import TEIReranker

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    transport = httpx.MockTransport(handler)
    r = TEIReranker("http://tei-rerank:80", transport=transport)
    with pytest.raises(RuntimeError) as exc_info:
        r.rerank("query", ["t0", "t1"])
    assert "http://tei-rerank:80" in str(exc_info.value)


def test_tei_reranker_connect_error_raises_runtime_error():
    from kbase.plugins.rerankers.tei import TEIReranker

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    transport = httpx.MockTransport(handler)
    r = TEIReranker("http://tei-rerank:80", transport=transport)
    with pytest.raises(RuntimeError) as exc_info:
        r.rerank("query", ["t0"])
    assert "http://tei-rerank:80" in str(exc_info.value)


def test_tei_reranker_registered_in_registry():
    import kbase.plugins.rerankers.tei  # noqa: F401
    from kbase.plugins.registry import registry

    r = registry.create("reranker", "tei", endpoint="http://tei-rerank:80",
                        transport=httpx.MockTransport(
                            lambda req: httpx.Response(200, json=[])))
    assert r is not None


# ---------------------------------------------------------------------------
# create_app 配置分支：embedder.name=tei / rerank.name=tei 缺 endpoint 时报错
# ---------------------------------------------------------------------------

_CFG_TEMPLATE = """
data_dir: {data_dir}
{embedder_block}
retrieval:
  rerank:
    {rerank_block}
llm:
  active: fake
  providers:
    - {{name: fake, base_url: 'http://x', api_key_env: FAKE_KEY, model: m}}
"""


def _write_cfg(tmp_path, *, embedder_block: str, rerank_block: str):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(
        _CFG_TEMPLATE.format(
            data_dir=str(tmp_path / "data").replace("\\", "/"),
            embedder_block=embedder_block, rerank_block=rerank_block),
        encoding="utf-8")
    return cfg


def test_create_app_embedder_tei_missing_endpoint_raises(tmp_path):
    from kbase.api.main import create_app

    cfg = _write_cfg(tmp_path, embedder_block="embedder:\n  name: tei",
                     rerank_block="enabled: false")
    with pytest.raises(ValueError, match="embedder.endpoint"):
        create_app(config_path=cfg, reranker=False, auth="off")


def test_create_app_rerank_tei_missing_endpoint_degrades(tmp_path):
    """reranker 构造失败走既有降级路径（不是本任务改动的行为）：
    ValueError 被 create_app 内部的 try/except 捕获，reranker=None 且
    /api/health 报 reranker: degraded，而不是让 create_app 直接抛出。"""
    from fastapi.testclient import TestClient

    from kbase.api.main import create_app

    cfg = _write_cfg(tmp_path, embedder_block="",
                     rerank_block="name: tei")

    class FakeEmbedder:
        dimension = 8

        def embed(self, texts):
            return [[0.0] * 8 for _ in texts]

    app = create_app(config_path=cfg, embedder=FakeEmbedder(), auth="off")
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["reranker"] == "degraded"
