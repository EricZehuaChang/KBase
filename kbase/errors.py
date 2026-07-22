"""统一业务错误（i18n 方案 A 的后端侧，spec §6）。业务代码
`raise AppError(code, message, status=..., **params)`，注册在 app 上的处理器
把它统一转成 HTTP 响应，detail 是结构化对象 `{code, params, message}`：

- **code**：语义点分 key（如 `error.kb_not_found`），前端 core.ts 拦截器据此
  查 i18n（用当前语言渲染，见 web-app/src/lib/api/core.ts）；
- **params**：插值参数（如 `{"id": kb_id}`），前端 `t(code, params)` 用；
- **message**：中文原文（已用 params 渲染好），前端查不到 key 时的兜底。

与 FastAPI 原生 `HTTPException(str)` 的 `{detail: str}` 并存——未迁移的端点
仍返回旧字符串，前端拦截器另有 `typeof raw === "string"` 分支照常显示。
渐进迁移：不要求一次全量 key 化（spec §6「未迁的仍返回旧字符串，前端兜底」）。
"""
from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """业务错误。`code` 是 i18n 语义 key；`message` 是中文模板（含 `{name}`
    占位，与前端 zh.json 基线同一套占位——vue-i18n 命名插值与 str.format 语法
    一致，两边复用同一模板）；`params` 供占位插值；`status` 是 HTTP 状态码。"""

    def __init__(self, code: str, message: str, *, status: int = 400, **params):
        self.code = code
        self.message_template = message
        self.params = params
        self.status = status
        # 渲染中文兜底消息（把 params 填进 {占位}）。缺参/多余占位不应炸出
        # 500——回退成原样模板，前端仍有 code 走 i18n 兜底。
        try:
            self.message = message.format(**params) if params else message
        except (KeyError, IndexError):
            self.message = message
        super().__init__(self.message)


def register_error_handler(app) -> None:
    """把 AppError 统一转成 `{detail: {code, params, message}}` 响应。detail 用
    dict 而非字符串——前端据 detail.code 查 i18n，查不到用 detail.message
    兜底（core.ts `"code" in raw` 分支）。在 create_app 装配路由前调用一次。"""

    @app.exception_handler(AppError)
    def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"detail": {"code": exc.code, "params": exc.params,
                                "message": exc.message}},
        )
