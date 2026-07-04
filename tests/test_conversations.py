import json

from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, MD, FakeLLM


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False)
    c = TestClient(app)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    return c, kb_id


def test_conversation_crud_and_title(tmp_path, fake_embedder):
    c, kb_id = _client(tmp_path, fake_embedder)
    conv = c.post("/api/conversations", json={"kb_id": kb_id}).json()
    assert conv["title"] == "新会话"
    q = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                  json={"question": q}) as r:
        body = "".join(r.iter_text())
    assert "event: done" in body
    convs = c.get("/api/conversations", params={"kb_id": kb_id}).json()
    assert convs[0]["title"] == q[:20]                     # 标题=首问前20字
    msgs = c.get(f"/api/conversations/{conv['id']}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"]                              # 助手消息已落库
    assert json.loads(msgs[1]["citations"])                # 引用已落库


def test_multi_turn_history_in_prompt(tmp_path, fake_embedder):
    c, kb_id = _client(tmp_path, fake_embedder)
    conv = c.post("/api/conversations", json={"kb_id": kb_id}).json()
    q = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                  json={"question": q}) as r:
        "".join(r.iter_text())
    # 第二轮：FakeLLM 记录 last_messages，历史应包含第一轮问答
    fake = c.app.state.test_llm                            # 见实现注记
    with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                  json={"question": q + "第二问"}) as r:
        "".join(r.iter_text())
    roles = [m["role"] for m in fake.last_messages]
    assert roles.count("user") >= 2                        # 历史 user + 当前 user
    assert roles[0] == "system"


def test_query_unknown_conversation_404(tmp_path, fake_embedder):
    c, _ = _client(tmp_path, fake_embedder)
    r = c.post("/api/conversations/nope/query", json={"question": "x"})
    assert r.status_code == 404
