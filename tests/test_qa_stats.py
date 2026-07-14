"""运营看板（C）：拒答落审计、问答量/拒答率概览、无答案问题清单。

FakeEmbedder 的向量是文本 md5，语义相似度不可控，无法靠"问无关问题"稳定
触发拒答。这里用确定性条件：空库（无任何文档）检索必空→必拒答；有文档的
库配 min_score_dense=-1（任何余弦≥-1）保证不拒答。两个库分别验证拒答/能答。
"""
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import MD, FakeLLM

# retrieval.min_score_dense=-1：有候选就不拒答（rerank 关，走 dense 阈值）
CFG_STATS = """
data_dir: {data_dir}
chunker: {{name: structure, chunk_size: 200, chunk_overlap: 0}}
retrieval: {{min_score_dense: -1.0}}
llm:
  active: fake
  providers:
    - {{name: fake, base_url: 'http://x', api_key_env: FAKE_KEY, model: m}}
"""


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG_STATS.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def _drain(c, kb_id, question):
    with c.stream("POST", f"/api/kb/{kb_id}/query",
                  json={"question": question, "top_k": 3}) as r:
        for _ in r.iter_lines():
            pass


def test_refusal_recorded_and_stats(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    doc_kb = c.post("/api/kb", json={"name": "有料库"}).json()["id"]
    c.post(f"/api/kb/{doc_kb}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    empty_kb = c.post("/api/kb", json={"name": "空库"}).json()["id"]

    _drain(c, doc_kb, "住房补贴能答")            # 有文档+阈值-1 → 能答
    _drain(c, empty_kb, "量子计算机散热方案")     # 空库 → 拒答
    _drain(c, empty_kb, "如何申请火星移民签证")   # 空库 → 拒答

    overview = c.get("/api/stats/qa").json()
    assert overview["total"] == 3
    assert overview["refused"] == 2
    assert overview["refusal_rate"] == round(2 / 3, 4)
    assert overview["trend"] and overview["trend"][-1]["total"] == 3

    questions = [i["question"] for i in c.get("/api/stats/unanswered").json()["items"]]
    assert questions[0] == "如何申请火星移民签证"     # 最近的在前
    assert "量子计算机散热方案" in questions
    assert "住房补贴能答" not in questions            # 能答的不进清单


def test_stats_empty(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    overview = c.get("/api/stats/qa").json()
    assert overview["total"] == 0 and overview["refusal_rate"] == 0.0
    assert c.get("/api/stats/unanswered").json()["items"] == []
