"""i18n 路由:UI 文案译文覆盖的读取与编辑(方案 A)。
- GET  /api/i18n/{lang}  **公开**(免登录分享页要用)——某语言覆盖 {key:value}
- GET  /api/i18n         admin——全部语言覆盖(管理页展示已改项)
- PUT  /api/i18n         admin——写某 key 某语言覆盖(value 空=删除回落基线)

公开读挂 app 级(绕过 /api 的 actor 鉴权),管理写挂 router(admin+审计),
与 share/openai_compat 同一"公开端点挂 app、受控端点挂 router"模式。译文
非敏感数据,公开读无风险且分享页/未登录终端用户必需。"""
from fastapi import Request

from kbase import i18n_store
from kbase.api.routes import RouteDeps
from kbase.api.schemas import TranslationPut
from kbase.api.services import Services


def register(app, router, svc: Services, deps: RouteDeps) -> None:
    sf = svc.sf

    @app.get("/api/i18n/{lang}")
    def get_i18n_overrides(lang: str):
        """某语言的 DB 覆盖(公开)。前端启动/切语言时合并进打包基线。"""
        return i18n_store.get_overrides(sf, lang)

    @router.get("/i18n", dependencies=[deps.require_admin])
    def list_i18n_overrides():
        """全部语言覆盖 {lang:{key:value}}(管理页),标出已改项。"""
        return i18n_store.get_all_overrides(sf)

    @router.put("/i18n", dependencies=[deps.require_admin, deps.audit_mutation])
    def put_i18n_override(body: TranslationPut, request: Request):
        """写译文覆盖(value 空=删除回落基线)。updated_by 记 actor 便于排查
        谁改的(审计中间件另有整体记录)。"""
        actor = getattr(request.state, "actor", None)
        who = actor.get("user_id") if actor else None
        result = i18n_store.set_override(sf, body.lang, body.key, body.value, who)
        return {"ok": True, "result": result}
