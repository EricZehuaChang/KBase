"""Prometheus /metrics 出口（D 运维）：文本格式、累计计数随问答递增。"""
from fastapi.testclient import TestClient

from kbase import metrics
from kbase.api.main import create_app
from tests.test_qa_stats import CFG_STATS
from tests.test_api import MD, FakeLLM


def test_render_prometheus_format():
    text = metrics.render(
        {"query_total": 5, "refused_total": 2, "login_failed_total": 1},
        {"rerank_total": 3, "rerank_shed_load_total": 1, "rerank_error_total": 0},
        "degraded")
    assert "# TYPE kbase_query_total counter" in text
    assert "kbase_query_total 5" in text
    assert "kbase_query_refused_total 2" in text
    assert "kbase_rerank_shed_load_total 1" in text
    assert "kbase_reranker_status -1" in text        # degraded → -1


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG_STATS.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def test_metrics_endpoint_counts_queries(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "kbase_query_total 0" in r.text
    assert "kbase_reranker_status 0" in r.text        # 未启用重排 → off → 0

    # 一次空库拒答问答后，两个 counter 各 +1
    kb = c.post("/api/kb", json={"name": "空库"}).json()["id"]
    with c.stream("POST", f"/api/kb/{kb}/query",
                  json={"question": "无依据的问题", "top_k": 3}) as resp:
        for _ in resp.iter_lines():
            pass
    text = c.get("/metrics").text
    assert "kbase_query_total 1" in text
    assert "kbase_query_refused_total 1" in text
