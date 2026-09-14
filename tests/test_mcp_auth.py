"""MCP 鉴权接入：env KBASE_API_KEY 设置时，KBaseClient 的每次反调请求都带
Authorization: Bearer 头；未设置且 API 要求鉴权时，工具返回清晰的中文错误
提示（指引设置 KBASE_API_KEY）。既有 test_mcp_tools.py/test_mcp_transport.py
的用例都跑在 auth="off" 的应用上（不涉及鉴权），这里补 auth="on" 端到端贯通。
"""
import httpx
from mcp.shared.memory import create_connected_server_and_client_session

from kbase_mcp.server import (KBaseClient, build_mcp, get_chunk_impl,
                              get_document_outline_impl,
                              list_knowledge_bases_impl,
                              submit_standard_answer_impl)
from tests.test_api import MD
from tests.test_auth import _client_on


def _make_auth_on_app(tmp_path, fake_embedder, monkeypatch):
    app, _c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                         monkeypatch=monkeypatch)
    return app


def _create_api_key(app, name="mcp-key", role="viewer") -> str:
    from fastapi.testclient import TestClient
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    r = c.post("/api/settings/api-keys", json={"name": name, "role": role})
    return r.json()["key"]


async def test_kbase_client_with_api_key_header_reaches_auth_on_app(
        tmp_path, fake_embedder, monkeypatch):
    """KBaseClient 的 httpx.AsyncClient 若带上 Authorization: Bearer <key>
    默认头，反调 auth="on" 应用的 /api/kb 应该成功（Bearer 通道鉴权贯通）；
    不带头则应该被拒 401。"""
    app = _make_auth_on_app(tmp_path, fake_embedder, monkeypatch)
    full_key = _create_api_key(app, role="viewer")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://kbase.test",
            headers={"Authorization": f"Bearer {full_key}"}) as http:
        c = KBaseClient(http)
        out = await list_knowledge_bases_impl(c)
        assert out == []     # 未 404/未 401，说明鉴权通过，走到了业务逻辑

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://kbase.test") as http_no_key:
        r = await http_no_key.get("/api/kb")
        assert r.status_code == 401


def test_default_client_headers_carry_bearer_when_env_set(monkeypatch):
    """未显式注入 client 时，build_mcp 用 build_default_client() 构造默认
    KBaseClient——该函数读 env KBASE_API_KEY，设置了就让每次反调请求自动
    带上 Authorization: Bearer 头，不要求调用方每次手工传 client。"""
    monkeypatch.setenv("KBASE_API_KEY", "kbase_ak_testtesttesttesttesttest01")
    from kbase_mcp.server import build_default_client
    client = build_default_client()
    assert client.http.headers["authorization"] == \
        "Bearer kbase_ak_testtesttesttesttesttest01"


def test_default_client_headers_no_auth_when_env_unset(monkeypatch):
    monkeypatch.delenv("KBASE_API_KEY", raising=False)
    from kbase_mcp.server import build_default_client
    client = build_default_client()
    assert "authorization" not in client.http.headers


async def test_list_knowledge_bases_401_hints_kbase_api_key(
        tmp_path, fake_embedder, monkeypatch):
    """未配置 KBASE_API_KEY（或配了错的）打到 auth="on" 的应用上会收到 401；
    工具应该识别这个 401 并包装成清晰的中文提示，指引设置 KBASE_API_KEY，
    而不是把裸的 401 HTML/JSON 错误体原样透传给调用方。"""
    app = _make_auth_on_app(tmp_path, fake_embedder, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://kbase.test") as http:
        c = KBaseClient(http)
        out = await list_knowledge_bases_impl(c)
        assert isinstance(out, dict) and "error" in out
        assert "KBASE_API_KEY" in out["error"]


async def test_mcp_tool_call_with_api_key_succeeds_over_auth_on_app(
        tmp_path, fake_embedder, monkeypatch):
    """更贴近真实使用场景：走 MCP session.call_tool，而不是直接调 impl 函数。"""
    app = _make_auth_on_app(tmp_path, fake_embedder, monkeypatch)
    full_key = _create_api_key(app, role="viewer")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://kbase.test",
            headers={"Authorization": f"Bearer {full_key}"}) as http:
        c = KBaseClient(http)
        fastmcp = build_mcp(c)
        async with create_connected_server_and_client_session(fastmcp) as session:
            result = await session.call_tool("list_knowledge_bases", {})
            assert result.structuredContent["result"] == []


# ---- T14：三个新工具的鉴权面 --------------------------------------------


def _seed_kb_and_chunk(app) -> tuple[str, str, str]:
    """admin 会话建库+传一份文档，返回 (kb_id, doc_id, chunk_id)。"""
    from fastapi.testclient import TestClient
    admin = TestClient(app)
    admin.post("/api/auth/login", json={"username": "admin",
                                        "password": "adminpass123"})
    kb_id = admin.post("/api/kb", json={"name": "政策库"}).json()["id"]
    files = {"files": ("补贴办法.md", MD.encode("utf-8"), "text/markdown")}
    assert admin.post(f"/api/kb/{kb_id}/documents", files=files).status_code == 200
    doc_id = admin.get(f"/api/kb/{kb_id}/documents").json()[0]["id"]
    chunk_id = admin.get(
        f"/api/documents/{doc_id}/chunks").json()["items"][0]["id"]
    return kb_id, doc_id, chunk_id


async def test_new_tools_401_hint_kbase_api_key(tmp_path, fake_embedder,
                                                monkeypatch):
    """新工具在未配置 KBASE_API_KEY、打到 auth="on" 的应用上同样收到 401；
    与既有契约一致，401 要包装成中文指引（而不是透传裸错误体）。"""
    app = _make_auth_on_app(tmp_path, fake_embedder, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://kbase.test") as http:
        c = KBaseClient(http)
        outs = [await get_chunk_impl(c, "chunk-x"),
                await get_document_outline_impl(c, "doc-x"),
                await submit_standard_answer_impl(c, "kb-x", "问题", "答案")]
    for out in outs:
        assert isinstance(out, dict) and "KBASE_API_KEY" in out["error"]


async def test_new_tools_over_mcp_session_with_api_key(tmp_path, fake_embedder,
                                                       monkeypatch):
    """带 API Key（editor：提交标问的门槛）走 MCP session 实调三个新工具：
    鉴权贯通、且三个工具都产出 structuredContent（返回标注是 list/dict 的
    Union 才会生成 output_schema，见 kbase_mcp/server.py 的注释）。"""
    app = _make_auth_on_app(tmp_path, fake_embedder, monkeypatch)
    full_key = _create_api_key(app, role="editor")
    kb_id, doc_id, chunk_id = _seed_kb_and_chunk(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://kbase.test",
            headers={"Authorization": f"Bearer {full_key}"}) as http:
        c = KBaseClient(http)
        fastmcp = build_mcp(c)
        async with create_connected_server_and_client_session(fastmcp) as session:
            got = await session.call_tool("get_chunk", {"chunk_id": chunk_id})
            assert got.structuredContent is not None
            assert got.structuredContent["result"]["doc_name"] == "补贴办法.md"

            outline = await session.call_tool("get_document_outline",
                                              {"doc_id": doc_id})
            assert outline.structuredContent is not None
            assert outline.structuredContent["result"][0]["title"] == "补贴办法"

            submitted = await session.call_tool(
                "submit_standard_answer",
                {"kb_id": kb_id, "question": "满几年可申领", "answer": "两年"})
            assert submitted.structuredContent is not None
            created = submitted.structuredContent["result"]
            assert created["kb_id"] == kb_id
            assert created["status"] == "pending_review"    # 只入审核队列


async def test_viewer_key_can_read_new_endpoints_but_not_submit(
        tmp_path, fake_embedder, monkeypatch):
    """角色边界：viewer key 能读两个新端点（require_viewer），但提交标问是
    editor 门槛（T13 的建标问端点）——工具把 403 原样透传成错误对象，而不是
    伪装成创建成功。"""
    app = _make_auth_on_app(tmp_path, fake_embedder, monkeypatch)
    kb_id, _doc_id, chunk_id = _seed_kb_and_chunk(app)
    viewer_key = _create_api_key(app, role="viewer")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://kbase.test",
            headers={"Authorization": f"Bearer {viewer_key}"}) as http:
        c = KBaseClient(http)
        assert (await get_chunk_impl(c, chunk_id))["doc_name"] == "补贴办法.md"
        out = await submit_standard_answer_impl(c, kb_id, "满几年", "两年")
    assert isinstance(out, dict) and "error" in out and "status" not in out

