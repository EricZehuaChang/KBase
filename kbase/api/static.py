"""双 SPA 静态资源托管：使用端/管理端两个前端 bundle 的回退路由。"""
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class SPAStaticFiles(StaticFiles):
    """双 SPA 回退路由（M5-1 F1）：真实静态资源正常返回；404 时按路径前缀回退
    到两个不同的入口 HTML，分别交给使用端/管理端各自独立的 vue-router 接管
    ——标准 FastAPI SPA 托管模式的双入口版本：
      - 路径为 "admin" 或以 "admin/" 开头（如 /admin/kb 这类管理端前端路由
        深链接）→ 回退 admin.html（管理端 bundle，见 web-app/admin.html）；
        用前缀匹配而不是简单的 in 判断，避免误伤形如 "/administrators" 这类
        恰好以 admin 打头但语义无关的路径。
      - 其余非文件 404（如 /kb 这类使用端路由深链接）→ 回退 index.html
        （使用端 bundle），与分端改造前行为完全一致。
    该 mount 挂在 "/"，未匹配到任何 API 路由的 /api/* 请求也会落到这里——
    必须显式排除，否则 /api/nonexistent 会被错误地回退成 200 的 index.html
    而不是 404，掩盖真正的路由错误。同理 /openapi.json、/docs、/redoc 在
    auth="on" 生产模式下被 FastAPI 关闭（docs_url=None 等），若不排除会被
    SPA 回退成 200 的 index.html——探测者拿不到 schema，但 200 状态会误导；
    显式排除让它们如实 404，鉴权加固才名副其实。
    注意：StaticFiles.get_response 未命中时是 raise HTTPException(404)，
    不是返回 404 响应，所以要 except 而不是检查返回值的状态码。"""

    async def get_response(self, path: str, scope: Scope) -> Response:
        # get_path() 用 os.path.join 拼出 path，Windows 上分隔符是反斜杠，
        # 用 PurePosixPath 统一成 "/" 再判断前缀，避免平台差异漏判。
        normalized = path.replace("\\", "/")
        if (normalized == "api" or normalized.startswith("api/")
                or normalized in ("healthz", "metrics")
                or normalized in ("openapi.json", "docs", "redoc")):
            return await super().get_response(path, scope)
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            if normalized == "admin" or normalized.startswith("admin/"):
                return await super().get_response("admin.html", scope)
            return await super().get_response("index.html", scope)
