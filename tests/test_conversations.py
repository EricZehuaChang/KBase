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


def test_same_tick_rounds_keep_order(tmp_path, fake_embedder, monkeypatch):
    """冻结时钟到同一时刻，连续多轮的消息顺序仍必须稳定。"""
    import kbase.conversations as convmod
    from datetime import datetime
    frozen = datetime(2026, 7, 5, 12, 0, 0)

    class FrozenDT(datetime):
        @classmethod
        def utcnow(cls):
            return frozen

    monkeypatch.setattr(convmod, "datetime", FrozenDT)
    c, kb_id = _client(tmp_path, fake_embedder)
    conv = c.post("/api/conversations", json={"kb_id": kb_id}).json()
    for i in range(3):
        with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                      json={"question": f"问题{i}"}) as r:
            "".join(r.iter_text())
    msgs = c.get(f"/api/conversations/{conv['id']}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"] * 3
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["问题0", "问题1", "问题2"]


def test_history_strips_citation_markers(tmp_path, fake_embedder):
    from kbase.conversations import append_round, build_history
    from kbase.db import make_session_factory
    sf = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    from kbase.models import Conversation
    with sf() as s:
        s.add(Conversation(id="cv1", kb_id="kb1"))
        s.commit()
    append_round(sf, "cv1", "问", "答案见[1]与[2]。", [], "p")
    hist = build_history(sf, "cv1")
    asst = next(m for m in hist if m["role"] == "assistant")
    assert "[1]" not in asst["content"] and "[2]" not in asst["content"]
