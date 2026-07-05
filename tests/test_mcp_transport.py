import httpx
import pytest

from kbase_mcp.server import KBaseClient, build_mcp
from mcp.shared.memory import create_connected_server_and_client_session

from kbase_mcp.__main__ import bearer_auth_middleware
from tests.test_api import CFG, MD, FakeLLM


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


async def test_ask_knowledge_base_populates_structured_content(tmp_path, fake_embedder):
    """搭车（Rider B）：FastMCP 对裸 `dict` 返回标注不生成 output_schema，
    CallToolResult.structuredContent 恒为 None（已用 func_metadata 探针确认）；
    ask_knowledge_base 的返回标注改为 `dict | list` 后应与 list_knowledge_bases
    一样在 structuredContent 里拿到 {"result": {...}}。这里起一个真实（fake
    组件）的 KBase ASGI 应用，走 STDIO 内存会话实调 ask，而不是只测 impl 函数，
    以验证 SDK 序列化路径本身确实被触发。"""
    from kbase.api.main import create_app

    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://kbase.test") as http:
        c = KBaseClient(http)
        r = await http.post("/api/kb", json={"name": "政策库"})
        kb_id = r.json()["id"]
        files = {"files": ("补贴办法.md", MD.encode("utf-8"), "text/markdown")}
        await http.post(f"/api/kb/{kb_id}/documents", files=files)

        fastmcp = build_mcp(c)
        q = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
        async with create_connected_server_and_client_session(fastmcp) as session:
            result = await session.call_tool(
                "ask_knowledge_base", {"kb_id": kb_id, "question": q})
            assert result.structuredContent is not None
            answer = result.structuredContent["result"]["answer"]
            assert "满两年" in answer


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
