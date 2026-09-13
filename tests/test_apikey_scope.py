"""API Key 库级 scope（ztenith MCP，P0-4）：受限 key 越权访问由服务端强制
静默处理——/search 返回空集、/query 返回与"检索无依据"完全一致的拒答流、
/api/kb 只列白名单库。不报错不提示：外部无法区分"库不存在/无权/真没答案"。
另含 MCP 工具 filters 参数透传的端到端（auth off 应用 + front matter 卡）。

T01/G01：/v1 OpenAI 兼容入口漏了同一道 scope 闸门——受限 key 可以列出并
检索白名单外的库。下面 test_openai_compat_* 一组钉住修复后的契约：/v1/models
不含白名单外的库、/v1/chat/completions 用 kb_id 或库名都返回 404
model_not_found，且**检索器从未被调用**（用 spy 记录调用，防"先检索再过滤"）。
"""

import httpx
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from kbase_mcp.server import KBaseClient, search_knowledge_impl
from tests.test_api import CFG, FakeLLM
from tests.test_auth import _client_on

CARD = """---
id: card-retail
industry: 零售
data_entities: [订单, 库存]
---
# 零售订单方案

订单列表页与库存同步的做法。
"""

PLAIN = """# 普通文档

没有元数据的内容。
"""


def _admin_client(app) -> TestClient:
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    return c


def _mk_two_kbs_with_docs(admin: TestClient) -> tuple[str, str]:
    kb1 = admin.post("/api/kb", json={"name": "允许库"}).json()["id"]
    kb2 = admin.post("/api/kb", json={"name": "禁区库"}).json()["id"]
    for kb in (kb1, kb2):
        r = admin.post(f"/api/kb/{kb}/documents",
                       files={"files": ("card.md", CARD.encode("utf-8"),
                                        "text/markdown")})
        assert r.status_code == 200
    return kb1, kb2


def _scoped_key(admin: TestClient, scope: list[str] | None, *,
                role: str = "viewer") -> str:
    r = admin.post("/api/settings/api-keys",
                   json={"name": f"scoped-{role}", "role": role,
                         "scope_kb_ids": scope})
    assert r.status_code == 200
    return r.json()["key"]


def test_scoped_key_silent_empty_on_out_of_scope(tmp_path, fake_embedder,
                                                 monkeypatch):
    app, _ = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    admin = _admin_client(app)
    kb1, kb2 = _mk_two_kbs_with_docs(admin)
    key = _scoped_key(admin, [kb1])

    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    # 白名单内：正常检索有命中
    ok = c.post(f"/api/kb/{kb1}/search", json={"query": "订单"})
    assert ok.status_code == 200 and ok.json()["blocks"]
    # 越权库：200 + 空集（不是 403/404——防探测），debug 形状同样为空
    denied = c.post(f"/api/kb/{kb2}/search", json={"query": "订单"})
    assert denied.status_code == 200 and denied.json() == {"blocks": []}
    denied_dbg = c.post(f"/api/kb/{kb2}/search",
                        json={"query": "订单", "debug": True})
    assert denied_dbg.json() == {"blocks": [], "trace": {}}
    # /api/kb 只列白名单库
    listed = c.get("/api/kb").json()
    assert [k["id"] for k in listed] == [kb1]
    # /query：拒答流语义（与"检索无依据"一致），无错误状态
    q = c.post(f"/api/kb/{kb2}/query", json={"question": "订单怎么做"})
    assert q.status_code == 200 and "未找到依据" in q.text
    # 白名单库 query 正常出答案（FakeLLM 固定回复）
    q_ok = c.post(f"/api/kb/{kb1}/query", json={"question": "订单怎么做"})
    assert q_ok.status_code == 200 and "未找到依据" not in q_ok.text


def test_unscoped_key_behaviour_unchanged(tmp_path, fake_embedder, monkeypatch):
    """不设 scope（NULL）＝不限，行为与升级前一致。"""
    app, _ = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    admin = _admin_client(app)
    kb1, kb2 = _mk_two_kbs_with_docs(admin)
    key = _scoped_key(admin, None)
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    assert {k["id"] for k in c.get("/api/kb").json()} == {kb1, kb2}
    assert c.post(f"/api/kb/{kb2}/search", json={"query": "订单"}).json()["blocks"]


async def test_mcp_search_filters_passthrough(tmp_path, fake_embedder):
    """MCP 工具带 filters 端到端：auth off 应用摄取一张卡+一份普通文档，
    filters={"industry": "零售"} 只命中卡内容。"""
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    c0 = TestClient(app)
    kb = c0.post("/api/kb", json={"name": "卡库"}).json()["id"]
    for name, text in [("card.md", CARD), ("plain.md", PLAIN)]:
        r = c0.post(f"/api/kb/{kb}/documents",
                    files={"files": (name, text.encode("utf-8"), "text/markdown")})
        assert r.status_code == 200

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://kbase.test") as http:
        mcp_client = KBaseClient(http)
        everything = await search_knowledge_impl(mcp_client, kb, "内容", top_k=10)
        assert {b["doc_name"] for b in everything} == {"card.md", "plain.md"}
        only_card = await search_knowledge_impl(
            mcp_client, kb, "内容", top_k=10, filters={"industry": "零售"})
        assert only_card and {b["doc_name"] for b in only_card} == {"card.md"}


# ---- T01/G01：/v1 OpenAI 兼容入口的库级 scope --------------------------


def _spy_retriever(app, calls: list[tuple]):
    """包一层 retrieve/retrieve_multi 记录调用（kb_id 参数），供断言"越权时
    检索器从未被调用"——只断言响应码无法区分"提前 404"与"检索后过滤"。"""
    retriever = app.state.svc.retriever
    real_retrieve, real_multi = retriever.retrieve, retriever.retrieve_multi

    def spy_retrieve(kb_id, query, *args, **kwargs):
        calls.append(("retrieve", kb_id))
        return real_retrieve(kb_id, query, *args, **kwargs)

    def spy_multi(kb_ids, query, *args, **kwargs):
        calls.append(("retrieve_multi", tuple(kb_ids)))
        return real_multi(kb_ids, query, *args, **kwargs)

    retriever.retrieve = spy_retrieve
    retriever.retrieve_multi = spy_multi


def _chat(c, model: str, *, stream: bool = False):
    return c.post("/v1/chat/completions", json={
        "model": model, "stream": stream,
        "messages": [{"role": "user", "content": "订单怎么做"}]})


def test_openai_compat_scope_filters_models_and_chat(tmp_path, fake_embedder,
                                                     monkeypatch):
    """受限 viewer key：/v1/models 不含白名单外库；用 kb_id 与**唯一库名**
    访问白名单外库都 404 model_not_found，且检索器从未被调用（流式与非流式
    都一样）。白名单内的库两种访问方式都正常。"""
    app, _ = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    admin = _admin_client(app)
    kb1, kb2 = _mk_two_kbs_with_docs(admin)          # kb1=允许库 kb2=禁区库
    calls: list[tuple] = []
    _spy_retriever(app, calls)

    key = _scoped_key(admin, [kb1])
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    # /v1/models：只列白名单内的库
    models = c.get("/v1/models")
    assert models.status_code == 200
    assert [m["id"] for m in models.json()["data"]] == [kb1]

    # 越权库：kb_id 与库名两条路径都 404 model_not_found（不泄漏存在性）
    for model in (kb2, "禁区库"):
        for stream in (False, True):
            r = _chat(c, model, stream=stream)
            assert r.status_code == 404, (model, stream, r.text)
            assert r.json()["error"]["code"] == "model_not_found"
    assert calls == [], f"越权请求不应触达检索器，实际调用: {calls}"

    # 白名单内的库：kb_id 与库名都能问答，引用有命中
    for model in (kb1, "允许库"):
        r = _chat(c, model)
        assert r.status_code == 200 and r.json()["citations"]
        assert calls[-1] == ("retrieve", kb1)

    # 原生入口语义不变：越权库仍是 200 + 静默空集（不是 404）
    native = c.post(f"/api/kb/{kb2}/search", json={"query": "订单"})
    assert native.status_code == 200 and native.json() == {"blocks": []}


def test_openai_compat_scope_applies_to_admin_key(tmp_path, fake_embedder,
                                                  monkeypatch):
    """受限 **admin** key 同样被 scope 挡住：kb_acl.can_access 对 admin 直接
    豁免 ACL，如果 scope 判定跟角色走，admin key 就能绕开白名单。"""
    app, _ = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    admin = _admin_client(app)
    kb1, kb2 = _mk_two_kbs_with_docs(admin)
    key = _scoped_key(admin, [kb1], role="admin")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    assert [m["id"] for m in c.get("/v1/models").json()["data"]] == [kb1]
    assert _chat(c, kb2).status_code == 404
    assert _chat(c, kb1).status_code == 200


def test_openai_compat_unscoped_and_revoked_keys(tmp_path, fake_embedder,
                                                 monkeypatch):
    """不设 scope 的 key 列出/访问全部库（升级前行为不变）；已吊销 key 在
    /v1 与 /api 都 401（凭据层就挡掉，与 scope 无关）。"""
    app, _ = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    admin = _admin_client(app)
    kb1, kb2 = _mk_two_kbs_with_docs(admin)

    open_key = _scoped_key(admin, None)
    c = TestClient(app, headers={"Authorization": f"Bearer {open_key}"})
    assert {m["id"] for m in c.get("/v1/models").json()["data"]} == {kb1, kb2}
    assert _chat(c, kb2).status_code == 200

    revoked_key = _scoped_key(admin, [kb1])
    listed = admin.get("/api/settings/api-keys").json()
    # 列表从不回传 key_hash/完整 key（只在创建那一刻返回一次）
    assert all("key_hash" not in k and "key" not in k for k in listed)
    victim = [k for k in listed if k["scope_kb_ids"] == [kb1]][0]
    assert admin.delete(f"/api/settings/api-keys/{victim['id']}").status_code == 200
    c2 = TestClient(app, headers={"Authorization": f"Bearer {revoked_key}"})
    assert c2.get("/v1/models").status_code == 401
    assert _chat(c2, kb1).status_code == 401
