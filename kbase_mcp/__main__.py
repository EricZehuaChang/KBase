"""KBase MCP Server 入口：STDIO（默认）或 Streamable HTTP 传输。

HTTP 传输下若设置 KBASE_MCP_TOKEN 环境变量，则用一个极简 ASGI 中间件
校验 `Authorization: Bearer <token>`，未带/带错 → 401。

未走 FastMCP 内置的 settings.auth（TokenVerifier/AuthSettings 那一整套 OAuth
资源服务器机制）——那是为标准 MCP 授权服务器场景设计的，这里只需要一个
共享密钥的简单校验，用中间件包一层 streamable_http_app() 更直接、依赖更少。
"""
import argparse
import hmac
import os

from kbase_mcp.server import build_mcp


def bearer_auth_middleware(app, token: str | None):
    """包一层 ASGI app：token 为 None 时直接放行（未启用鉴权）；
    否则校验 Authorization: Bearer <token>，不符则 401。"""
    if token is None:
        return app

    expected = f"Bearer {token}".encode("latin-1")

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization")
        # 未带头直接 401；带头则用 hmac.compare_digest 做常数时间比较，
        # 避免普通 != 的短路比较泄露前缀匹配长度（时序侧信道）。
        if auth is None or not hmac.compare_digest(auth, expected):
            body = b'{"error": "unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await app(scope, receive, send)

    return wrapped


def main() -> None:
    parser = argparse.ArgumentParser(prog="kbase_mcp",
                                     description="KBase MCP Server")
    parser.add_argument("--http", action="store_true",
                        help="使用 Streamable HTTP 传输（默认 STDIO）")
    parser.add_argument("--port", type=int, default=3001)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    mcp = build_mcp()

    if not args.http:
        mcp.run()  # STDIO，默认 transport
        return

    import uvicorn

    mcp.settings.host = args.host
    mcp.settings.port = args.port

    token = os.environ.get("KBASE_MCP_TOKEN")
    app = bearer_auth_middleware(mcp.streamable_http_app(), token=token)

    uvicorn.run(app, host=args.host, port=args.port,
               log_level=mcp.settings.log_level.lower())


if __name__ == "__main__":
    main()
