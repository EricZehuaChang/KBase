"""HTTP 装配层（组合根）：create_app 只做四件事——构建核心服务
（kbase/api/services.py）、装配鉴权、把各领域路由（kbase/api/routes/）挂上
共享 router、托管双 SPA 静态资源。业务逻辑在 ingest/rag/jobs 各模块，
请求体 schema 在 kbase/api/schemas.py。

uvicorn 工厂路径保持不变：`uvicorn --factory kbase.api.main:create_app`。"""
from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, FastAPI, Response

from kbase.api.routes import RouteDeps
from kbase.api.routes import (admin as admin_routes, auth as auth_routes,
                              evals as evals_routes, jobs as jobs_routes,
                              kb as kb_routes,
                              openai_compat as openai_routes,
                              query as query_routes,
                              settings as settings_routes)
from kbase.api.services import build_services
from kbase.api.static import SPAStaticFiles
from kbase.errors import register_error_handler
from kbase.audit import make_mutation_audit_dependency
from kbase.auth import security
from kbase.auth.bootstrap import ensure_admin
from kbase.auth.deps import (make_get_current_actor, make_origin_guard_middleware,
                             make_synthetic_admin_actor_dependency, require_role)


def create_app(config_path="config/kbase.yaml", *, embedder=None,
               store=None, llms: dict | None = None, reranker=None,
               enricher=None, ocr_backend=None, rewriter=None,
               auth: str = "on") -> FastAPI:
    """auth: "on"（生产默认）——全部 /api 路由（除 POST /api/auth/login 外）
    要求有效的会话 Cookie 或 API Key，启动时执行首启 admin 引导；
    "off"（既有功能测试用）——跳过 actor 依赖与 bootstrap，行为与鉴权改造前
    完全一致，被测物是功能而不是鉴权。
    reranker: None=按配置加载（生产默认，失败自动降级）；
    False=显式关闭（测试用哨兵，保持既有分数语义不变）；
    传实例=直接注入（测试用 fake reranker）。
    enricher: None=生产默认，包一层 LazyEnricher 延迟到首次摄取时才解析真实
    LLM（避免未启用 enrich 的部署也要求配好密钥）；
    False=显式关闭（测试哨兵，pipeline 不做任何增强）；
    传实例=直接注入（测试用真实 ContextualEnricher 或 fake，跳过 Lazy 包装）。
    ocr_backend: None=按配置加载（cfg.ocr.enabled 为真时创建 monkey-http 后端，
    生产默认；未启用则不装 OCR，扫描件/图片直接判 failed）；
    传实例=直接注入（测试用 FakeOCR，跳过配置读取与真实网络依赖）。
    rewriter: None=生产默认，包一层 LazyRewriter 延迟到首次会话查询时才解析
    真实 QueryRewriter（避免未用到改写的部署也要求配好改写 provider 密钥）；
    False=显式关闭（测试哨兵/等价 mode=off，会话查询检索用原文，行为与 M2
    完全一致）；传实例=直接注入（测试用真实 QueryRewriter 搭配 FakeLLM，跳过
    Lazy 包装）。仅会话查询路由（/api/conversations/{id}/query）使用；
    旧的 /api/kb/{id}/query 端点不接入改写，行为字节级不变。"""
    # 严格校验 auth，杜绝拼写错误（如 "On"/"true"/"disabled"）静默落到与
    # 意图相反的分支——尤其是任何非 "on" 的值都会走 off 路径、悄悄关闭鉴权。
    if auth not in ("on", "off"):
        raise ValueError(f"auth must be 'on' or 'off', got {auth!r}")

    svc = build_services(config_path, embedder=embedder, store=store,
                         llms=llms, reranker=reranker, enricher=enricher,
                         ocr_backend=ocr_backend, rewriter=rewriter)
    cfg = svc.cfg

    # auth="on"（生产）：关闭 /docs、/redoc、/openapi.json——它们默认不鉴权，
    # 会把完整路由与模型 schema 暴露给未认证访问者，生产环境不应可达；
    # auth="off"（dev/test）：保留，方便本地查阅交互式文档。
    if auth == "on":
        app = FastAPI(title="KBase", docs_url=None, redoc_url=None,
                      openapi_url=None)
    else:
        app = FastAPI(title="KBase")

    # 业务错误统一处理（i18n 方案 A，spec §6）：AppError → detail={code,params,
    # message}，前端 core.ts 拦截器据 code 本地化、查不到用 message 兜底。未迁移
    # 端点的 HTTPException(str) 不受影响（前端另有字符串兜底分支）。
    register_error_handler(app)

    # M4-2 H7：AnyIO 的默认线程池容量（run_in_threadpool 用它执行 retrieve()
    # 等同步阻塞调用）绑定在当前事件循环上，且 create_app() 本身是同步函数、
    # 运行时还没有事件循环——current_default_thread_limiter() 在无循环上下文
    # 调用会抛 NoEventLoopError（本地验证过）。必须挪到 startup 钩子里，
    # 届时 uvicorn 已经启动了事件循环。cfg.server.threadpool_size 与 AnyIO
    # 默认值（40）相同时不做任何调用，做到"不配置=零行为变化"。
    if cfg.server.threadpool_size != 40:
        @app.on_event("startup")
        def _configure_threadpool_size() -> None:
            limiter = anyio.to_thread.current_default_thread_limiter()
            limiter.total_tokens = cfg.server.threadpool_size

    # 测试注入路径：暴露被注入的 active llm 实例，便于测试直接断言其记录的
    # last_messages（如 FakeLLM），而不必依赖内部私有变量；生产路径未注入
    # 任何 llm 时为 None。
    app.state.test_llm = svc.test_llm

    # ---- 鉴权装配（spec §2/§7，角色矩阵+审计见 spec §3/§5，G3）----
    # auth="off"：既有功能测试路径，router 级依赖换成 synthetic_admin_actor
    # ——不校验任何凭据，直接把 request.state.actor 设成 rank 最高的合成
    # admin，不跑 bootstrap、不加 Origin 中间件。各路由上仍挂着与 auth="on"
    # 完全相同的 require_role(min_role) 声明，但因为合成 actor 总是 admin，
    # 角色矩阵在这条路径下是无操作——行为与鉴权改造前完全一致。
    # auth="on"（生产默认）：router 级依赖是真正的 get_current_actor（Cookie
    # 会话或 Bearer API Key），首启自动引导 admin；非 GET 请求校验 Origin
    # 同源；各路由的 require_role(min_role) 依赖据此校验真实角色。
    secret = security.resolve_secret_key(svc.sf)
    get_current_actor = make_get_current_actor(svc.sf, secret=secret)
    if auth == "on":
        ensure_admin(svc.sf)
        app.middleware("http")(make_origin_guard_middleware())
        actor_dependency = get_current_actor
    else:
        actor_dependency = make_synthetic_admin_actor_dependency()
    router = APIRouter(prefix="/api", dependencies=[Depends(actor_dependency)])

    # 各路由的最低角色依赖，按 spec §3 表 + 落地细则预先建好（viewer < editor < admin）：
    # viewer：只读 GET 与问答/检索/会话查询 POST；
    # editor：内容管理类 mutating 端点（建库/上传/删文档/删库/kb 配置/发起生成/建会话）；
    # admin：settings/*（Provider 等）与审计查询。
    # mutating 请求审计钩子挂在 require_role 之后（Depends 按声明顺序解析），
    # 保证 403 被拒绝的请求不落审计行。
    deps = RouteDeps(
        require_viewer=Depends(require_role("viewer")),
        require_editor=Depends(require_role("editor")),
        require_admin=Depends(require_role("admin")),
        audit_mutation=Depends(make_mutation_audit_dependency(svc.sf)),
    )

    def _reranker_status() -> str:
        if svc.rerank_degraded:
            return "degraded"
        return "on" if svc.retriever.rerank_active else "off"

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "embedder": type(svc.embedder).__name__,
                "vectorstore": type(svc.store).__name__,
                "reranker": _reranker_status(),
                "rerank_stats": svc.retriever.rerank_stats}

    # /metrics（D 运维）：Prometheus 文本出口，与 /healthz 同为 app 级、无鉴权
    # ——生产靠网络隔离而非 token 保护抓取端点，客户 Prometheus 可直接 scrape。
    @app.get("/metrics")
    def metrics() -> Response:
        from kbase import metrics as metrics_mod
        from kbase.qa_stats import lifetime_counters
        body = metrics_mod.render(lifetime_counters(svc.sf),
                                  svc.retriever.rerank_stats, _reranker_status())
        return Response(content=body, media_type="text/plain; version=0.0.4")

    # 各领域路由注册：auth（登录/登出/身份）、admin（用户/API Key/审计/许可证）、
    # settings（Provider 管理）、kb（知识库与文档）、query（问答/检索/会话）、
    # jobs（大纲/长任务/产物）。路径互不重叠，注册顺序不影响匹配。
    auth_routes.register(app, router, svc, deps, secret=secret)
    admin_routes.register(router, svc, deps)
    settings_routes.register(router, svc, deps)
    kb_routes.register(router, svc, deps)
    run_query = query_routes.register(router, svc, deps)
    jobs_routes.register(router, svc, deps)
    evals_routes.register(router, svc, deps)

    from kbase.api.routes import share as share_routes
    share_routes.register(app, router, svc, deps, run_query=run_query)
    from kbase.api.routes import feishu_bot as feishu_bot_routes
    feishu_bot_routes.register(app, router, svc, deps)
    from kbase.api.routes import connectors as connectors_routes
    connector_sync = connectors_routes.register(router, svc, deps)
    from kbase.api.routes import i18n as i18n_routes
    i18n_routes.register(app, router, svc, deps)
    app.include_router(router)

    # 连接器定时同步调度器（对标#3）：startup 起 daemon 线程（TestClient
    # 非 with 用法不触发 startup——既有测试零线程；uvicorn 生产路径正常
    # 启动）。启动前先复位崩溃残留的 running 状态，否则该连接器永远抢不
    # 到锁。shutdown 响应式停线程。
    from kbase.connectors import ConnectorScheduler, reset_stale_running
    scheduler = ConnectorScheduler(svc.sf, connector_sync)
    app.state.connector_scheduler = scheduler

    @app.on_event("startup")
    def _start_connector_scheduler() -> None:
        reset_stale_running(svc.sf)
        scheduler.start()

    @app.on_event("shutdown")
    def _stop_connector_scheduler() -> None:
        scheduler.stop()

    # M6-5 OpenAI 兼容 API：挂在 /v1（不在 /api 前缀下），鉴权与 /api 相同
    # （Bearer API Key / 会话 Cookie），供 OpenAI 生态客户端零改造接入。
    openai_routes.register(app, svc, actor_dependency)

    web_dir = Path(__file__).resolve().parents[2] / "web"
    if web_dir.exists():
        app.mount("/", SPAStaticFiles(directory=str(web_dir), html=True), name="web")
    return app
