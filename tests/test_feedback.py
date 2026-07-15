"""反馈闭环（M6-4）：点赞点踩落库、覆盖式更新、差评清单带问题原文、
非助手消息/不存在消息 404。"""
import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM


@pytest.fixture
def chat_env(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    c = TestClient(app)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb}/documents",
           files=[("files", ("补贴.md", "# 补贴\n住房补贴满两年可申领。".encode("utf-8"),
                             "text/markdown"))])
    conv = c.post("/api/conversations", json={"kb_id": kb}).json()
    # 走一轮问答落两条消息（user+assistant）
    with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                  json={"question": "住房补贴怎么申领？"}) as r:
        for _ in r.iter_lines():
            pass
    msgs = c.get(f"/api/conversations/{conv['id']}/messages").json()
    return c, msgs


def test_feedback_upsert_and_negative_list(chat_env):
    c, msgs = chat_env
    user_msg = next(m for m in msgs if m["role"] == "user")
    assistant_msg = next(m for m in msgs if m["role"] == "assistant")

    # 点赞
    r = c.post(f"/api/messages/{assistant_msg['id']}/feedback", json={"rating": 1})
    assert r.status_code == 200 and r.json()["rating"] == 1
    stats = c.get("/api/stats/feedback").json()
    assert stats["up"] == 1 and stats["down"] == 0

    # 改主意点踩（覆盖，不是双记录）
    c.post(f"/api/messages/{assistant_msg['id']}/feedback",
           json={"rating": -1, "note": "引用的版本是旧的"})
    stats = c.get("/api/stats/feedback").json()
    assert stats["up"] == 0 and stats["down"] == 1
    # 差评清单带问题原文与备注
    item = stats["items"][0]
    assert item["question"] == user_msg["content"]
    assert item["note"] == "引用的版本是旧的"
    assert item["answer_excerpt"]

    # user 消息不可评、不存在消息 404
    assert c.post(f"/api/messages/{user_msg['id']}/feedback",
                  json={"rating": 1}).status_code == 404
    assert c.post("/api/messages/nope/feedback",
                  json={"rating": 1}).status_code == 404
    # rating 只收 1/-1
    assert c.post(f"/api/messages/{assistant_msg['id']}/feedback",
                  json={"rating": 5}).status_code == 422
