"""评测回归（B）：建集校验、一键回归 hit@k/MRR 计算、历史对比、删除级联、
无期望用例拒收。"""
import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


@pytest.fixture
def kb_ready(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb}/documents", files=[
        ("files", ("补贴.md", "# 补贴\n住房补贴入职满两年可申领。".encode("utf-8"),
                   "text/markdown")),
        ("files", ("考勤.md", "# 考勤\n迟到三次记旷工半天。".encode("utf-8"),
                   "text/markdown")),
    ])
    return c, kb


def test_create_and_run_eval(kb_ready):
    c, kb = kb_ready
    r = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "冒烟集",
        "cases": [
            {"question": "住房补贴怎么申领", "expect_doc": "补贴.md"},
            {"question": "迟到怎么处理", "expect_text": "旷工半天"},
            {"question": "年终奖发多少", "expect_doc": "不存在.md"},   # 永不命中
        ]})
    assert r.status_code == 200, r.text
    set_id = r.json()["id"]
    assert r.json()["case_count"] == 3

    run = c.post(f"/api/eval-sets/{set_id}/run", json={"top_k": 5}).json()
    assert run["total"] == 3
    # 前两条应命中（fake embedder 确定性向量 + BM25 关键词路），第三条不可能中
    assert run["hits"] == 2
    assert run["hit_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert 0 < run["mrr"] <= 1
    miss = next(d for d in run["details"] if not d["hit"])
    assert miss["question"] == "年终奖发多少" and miss["rank"] is None

    # 历史对比：再跑一次 → 两行，倒序
    c.post(f"/api/eval-sets/{set_id}/run", json={"top_k": 5})
    runs = c.get(f"/api/eval-sets/{set_id}/runs").json()
    assert len(runs) == 2
    assert runs[0]["created_at"] >= runs[1]["created_at"]

    # 单次明细可回查
    detail = c.get(f"/api/eval-runs/{run['id']}").json()
    assert len(detail["details"]) == 3


def test_case_requires_expectation(kb_ready):
    c, kb = kb_ready
    r = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "坏集", "cases": [{"question": "没期望的用例"}]})
    assert r.status_code == 422


def test_delete_set_cascades_runs(kb_ready):
    c, kb = kb_ready
    set_id = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "集", "cases": [{"question": "q", "expect_text": "补贴"}]}).json()["id"]
    run_id = c.post(f"/api/eval-sets/{set_id}/run", json={}).json()["id"]
    assert c.delete(f"/api/eval-sets/{set_id}").json()["ok"] is True
    assert c.get(f"/api/eval-sets/{set_id}/runs").status_code == 404
    assert c.get(f"/api/eval-runs/{run_id}").status_code == 404
    assert c.get(f"/api/kb/{kb}/eval-sets").json() == []
