"""API Key 库级 scope（ztenith MCP，P0-4）：受限 key 越权访问由服务端强制
静默处理——/search 返回空集、/query 返回与"检索无依据"完全一致的拒答流、
/api/kb 只列白名单库。不报错不提示：外部无法区分"库不存在/无权/真没答案"。
另含 MCP 工具 filters 参数透传的端到端（auth off 应用 + front matter 卡）。
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


def _scoped_key(admin: TestClient, scope: list[str] | None) -> str:
    r = admin.post("/api/settings/api-keys",
                   json={"name": "scoped", "role": "viewer",
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
