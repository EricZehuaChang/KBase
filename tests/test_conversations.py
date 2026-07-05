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
    assert convs["items"][0]["title"] == q[:20]            # 标题=首问前20字
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


class FakeRewriteLLM:
    """QueryRewriter 专用假 LLM：把追问改写为含关键词的自包含问题。"""
    def __init__(self, out="出差北京司局级住宿费标准是多少"):
        self.out = out
        self.calls = 0

    async def complete(self, messages, **params):
        self.calls += 1
        return self.out


def _client_with_rewriter(tmp_path, fake_embedder, rewriter):
    """conversations 测试自有的 client 构造：注入 SpyEmbedder 记录检索用的实际
    query 文本，并直接传入 QueryRewriter 实例（同 reranker/enricher 的实例注入
    哨兵模式），跳过配置文件里的 provider 懒解析。"""
    calls = []

    class SpyEmbedder:
        dimension = fake_embedder.dimension

        def embed(self, texts):
            calls.append(list(texts))
            return fake_embedder.embed(texts)

    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    spy = SpyEmbedder()
    app = create_app(config_path=cfg, embedder=spy,
                     llms={"fake": FakeLLM()}, reranker=False,
                     rewriter=rewriter)
    c = TestClient(app)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    return c, kb_id, calls


def test_rewriter_rewrites_retrieval_but_not_generation(tmp_path, fake_embedder):
    from kbase.rag.rewriter import QueryRewriter
    rewriter = QueryRewriter(llm=FakeRewriteLLM(), mode="always")
    c, kb_id, embed_calls = _client_with_rewriter(tmp_path, fake_embedder, rewriter)
    conv = c.post("/api/conversations", json={"kb_id": kb_id}).json()
    q1 = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                  json={"question": q1}) as r:
        "".join(r.iter_text())
    embed_calls.clear()                     # 只看第二轮（追问）触发的检索
    q2 = "那司局级呢？"
    with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                  json={"question": q2}) as r:
        "".join(r.iter_text())
    # 检索路（SpyEmbedder）应看到改写后的关键词
    assert any("司局级" in t and "住宿费" in t for call in embed_calls for t in call)
    # 生成 LLM 收到的最后一条 user 消息仍是原始短问（展示/落库用原文）
    fake = c.app.state.test_llm
    last_user = [m for m in fake.last_messages if m["role"] == "user"][-1]
    assert q2 in last_user["content"]
    assert "出差北京司局级住宿费标准是多少" not in last_user["content"]
    # 落库也是原文
    msgs = c.get(f"/api/conversations/{conv['id']}/messages").json()
    user_msgs = [m["content"] for m in msgs if m["role"] == "user"]
    assert user_msgs[-1] == q2


def test_conversation_pagination(tmp_path, fake_embedder):
    """GET /api/conversations 分页：默认 limit=30，响应 {items, total}；
    造 35 条会话分页取两页，覆盖全部且不重复。"""
    c, kb_id = _client(tmp_path, fake_embedder)
    for _ in range(35):
        c.post("/api/conversations", json={"kb_id": kb_id})

    page1 = c.get("/api/conversations", params={"kb_id": kb_id}).json()
    assert page1["total"] == 35
    assert len(page1["items"]) == 30           # 默认 limit=30

    page2 = c.get("/api/conversations",
                  params={"kb_id": kb_id, "limit": 30, "offset": 30}).json()
    assert page2["total"] == 35
    assert len(page2["items"]) == 5

    ids_p1 = {i["id"] for i in page1["items"]}
    ids_p2 = {i["id"] for i in page2["items"]}
    assert not (ids_p1 & ids_p2)               # 两页不重叠
    assert len(ids_p1 | ids_p2) == 35          # 覆盖全部


def test_conversation_limit_capped_at_100(tmp_path, fake_embedder):
    c, kb_id = _client(tmp_path, fake_embedder)
    for _ in range(3):
        c.post("/api/conversations", json={"kb_id": kb_id})
    r = c.get("/api/conversations", params={"kb_id": kb_id, "limit": 500})
    assert r.status_code == 422                # 上限 100，超出应被拒绝


def test_rewriter_false_matches_m2_behavior(tmp_path, fake_embedder):
    """rewriter=False 显式关闭时行为与 M2（无改写）完全一致：检索用原文。"""
    c, kb_id, embed_calls = _client_with_rewriter(tmp_path, fake_embedder, False)
    conv = c.post("/api/conversations", json={"kb_id": kb_id}).json()
    q1 = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                  json={"question": q1}) as r:
        "".join(r.iter_text())
    embed_calls.clear()
    q2 = "那司局级呢？"
    with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                  json={"question": q2}) as r:
        "".join(r.iter_text())
    assert any(q2 in t for call in embed_calls for t in call)
