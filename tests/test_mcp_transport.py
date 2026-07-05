import httpx
import pytest

from kbase_mcp.server import KBaseClient, build_mcp
from mcp.shared.memory import create_connected_server_and_client_session

from kbase_mcp.__main__ import bearer_auth_middleware


EXPECTED_TOOLS = {"list_knowledge_bases", "search_knowledge", "ask_knowledge_base"}


@pytest.fixture
async def dead_client():
    c = httpx.AsyncClient(base_url="http://127.0.0.1:1")  # 无监听端口
    yield KBaseClient(c)
    await c.aclose()


async def test_stdio_handshake_lists_three_tools_with_chinese_descriptions(dead_client):
    fastmcp = build_mcp(dead_client)
    async with create_connected_server_and_client_session(fastmcp) as session:
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert names == EXPECTED_TOOLS
        for t in tools.tools:
            assert t.description and any(ord(ch) > 127 for ch in t.description)


async def test_stdio_handshake_call_tool_returns_error_face_when_api_down(dead_client):
    """API 未启动也算通——错误面即契约。"""
    fastmcp = build_mcp(dead_client)
    async with create_connected_server_and_client_session(fastmcp) as session:
        result = await session.call_tool("list_knowledge_bases", {})
        assert result.structuredContent["result"]["error"]
        assert "KBase 服务" in result.structuredContent["result"]["error"]


async def _asgi_app(scope, receive, send):
    body = b'{"ok": true}'
    await send({"type": "http.response.start", "status": 200,
                 "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


async def test_bearer_middleware_rejects_missing_header():
    wrapped = bearer_auth_middleware(_asgi_app, token="secret")
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/mcp")
        assert r.status_code == 401


async def test_bearer_middleware_rejects_wrong_token():
    wrapped = bearer_auth_middleware(_asgi_app, token="secret")
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/mcp", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401


async def test_bearer_middleware_passes_correct_token():
    wrapped = bearer_auth_middleware(_asgi_app, token="secret")
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/mcp", headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200
        assert r.json() == {"ok": True}


async def test_bearer_middleware_noop_when_token_none():
    """KBASE_MCP_TOKEN 未设置时不做校验（None 表示不启用）。"""
    wrapped = bearer_auth_middleware(_asgi_app, token=None)
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/mcp")
        assert r.status_code == 200
