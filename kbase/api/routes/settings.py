"""Provider 设置域路由：LLM provider 的增删改查、默认切换、连通性测试
与模型目录（各厂商 /models 清单的拉取与缓存）。

providers 表以 DB 为唯一真源（首次启动由 YAML seed，见 services.py）；
任何 PUT/DELETE 后必须使 LLM 实例缓存失效（svc.invalidate_llm_cache），
下次 get_llm 按 DB 最新定义重建。"""
import asyncio
import logging
import os
import time

from fastapi import BackgroundTasks, HTTPException

from kbase import model_catalog, providers_store
from kbase.api.routes import RouteDeps
from kbase.api.schemas import (ActiveProviderBody, EmbedderKeyBody,
                               FeishuCredentialsBody, ModelRefreshBody,
                               ProviderCreate, ProviderUpdate,
                               SmtpSettingsBody, SmtpTestBody)
from kbase.api.services import Services

logger = logging.getLogger(__name__)


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf = svc.sf

    @router.get("/providers", dependencies=[deps.require_viewer])
    def providers():
        # 旧端点（Plan B 前旧前端仍用它）：改读 DB，返回结构保持不变
        return {"active": providers_store.get_active(sf),
                "providers": [p["name"] for p in providers_store.list_providers(sf)]}

    @router.get("/settings/providers", dependencies=[deps.require_admin])
    def settings_list_providers():
        # 脱敏视图：api_key 原文永不出站，只回 has_api_key + 尾4位提示
        return {"active": providers_store.get_active(sf),
                "providers": [providers_store.to_public(p)
                              for p in providers_store.list_providers(sf)]}

    @router.post("/settings/providers",
                 dependencies=[deps.require_admin, deps.audit_mutation])
    def settings_create_provider(body: ProviderCreate):
        if providers_store.get_provider_dict(sf, body.name) is not None:
            raise HTTPException(409, f"provider 已存在: {body.name}")
        providers_store.create_provider(sf, body.model_dump())
        return {"ok": True}

    @router.put("/settings/providers/{name}",
                dependencies=[deps.require_admin, deps.audit_mutation])
    def settings_update_provider(name: str, body: ProviderUpdate):
        found = providers_store.update_provider(
            sf, name, body.model_dump(exclude_unset=True))
        if not found:
            raise HTTPException(404, f"provider 不存在: {name}")
        svc.invalidate_llm_cache(name)
        return {"ok": True}

    @router.delete("/settings/providers/{name}",
                   dependencies=[deps.require_admin, deps.audit_mutation])
    def settings_delete_provider(name: str):
        if providers_store.get_active(sf) == name:
            raise HTTPException(409, "默认 provider 不可删除，请先切换默认")
        found = providers_store.delete_provider(sf, name)
        if not found:
            raise HTTPException(404, f"provider 不存在: {name}")
        svc.invalidate_llm_cache(name)
        return {"ok": True}

    # ---- 向量模型密钥页面配置：DB 覆盖 > api_key_env，改/清后丢缓存实例 ----

    @router.get("/settings/embedder-keys", dependencies=[deps.require_admin])
    def list_embedder_keys():
        """cfg.embedders 中云端向量选项（openai-embed）的密钥脱敏状态。"""
        from kbase import embedder_keys
        return {"items": embedder_keys.list_status(sf, svc.cfg)}

    @router.put("/settings/embedder-keys/{option_id}",
                dependencies=[deps.require_admin, deps.audit_mutation])
    def put_embedder_key(option_id: str, body: EmbedderKeyBody):
        from kbase import embedder_keys
        known = {o.id for o in svc.cfg.embedders if o.plugin == "openai-embed"}
        if option_id not in known:
            raise HTTPException(404, f"向量模型选项不存在或无密钥概念: {option_id}")
        embedder_keys.set_key(sf, option_id, body.api_key)
        svc.embedder_pool.invalidate(option_id)   # 下次使用按新密钥重建
        return {"ok": True}

    @router.delete("/settings/embedder-keys/{option_id}",
                   dependencies=[deps.require_admin, deps.audit_mutation])
    def delete_embedder_key(option_id: str):
        from kbase import embedder_keys
        if not embedder_keys.delete_key(sf, option_id):
            raise HTTPException(404, f"该选项未配置页面密钥: {option_id}")
        svc.embedder_pool.invalidate(option_id)   # 回落到 api_key_env
        return {"ok": True}

    # ---- 发件箱（SMTP，账号通知/系统邮件；密码只写不回显） ----

    @router.get("/settings/smtp", dependencies=[deps.require_admin])
    def get_smtp():
        from kbase import mailer
        return mailer.status(sf)

    @router.put("/settings/smtp",
                dependencies=[deps.require_admin, deps.audit_mutation])
    def put_smtp(body: SmtpSettingsBody):
        from kbase import mailer
        mailer.set_settings(sf, host=body.host.strip(), port=body.port,
                            user=body.user.strip(), password=body.password,
                            from_addr=body.from_addr.strip(),
                            from_name=body.from_name.strip() or "KBase")
        return {"ok": True}

    @router.post("/settings/smtp/test",
                 dependencies=[deps.require_admin, deps.audit_mutation])
    def test_smtp(body: SmtpTestBody):
        """发一封测试邮件验证配置连通（同步等结果，失败给可读原因）。"""
        from kbase import mailer
        try:
            mailer.send_mail(sf, body.to.strip(), "KBase 发件箱测试",
                             "这是一封来自 KBase 的测试邮件。收到即说明发件箱配置正确。")
        except Exception as e:  # noqa: BLE001 —— SMTP 侧错误转可读信息
            raise HTTPException(502, f"发送失败: {e}") from e
        return {"ok": True}

    # ---- 飞书连接器凭据（页面维护，secret 脱敏） ----

    @router.get("/settings/feishu", dependencies=[deps.require_admin])
    def get_feishu_credentials():
        from kbase import feishu
        return feishu.credentials_status(sf)

    @router.put("/settings/feishu",
                dependencies=[deps.require_admin, deps.audit_mutation])
    def put_feishu_credentials(body: FeishuCredentialsBody):
        from kbase import feishu
        feishu.set_credentials(sf, body.app_id.strip(), body.app_secret.strip())
        return {"ok": True}

    @router.delete("/settings/feishu",
                   dependencies=[deps.require_admin, deps.audit_mutation])
    def delete_feishu_credentials():
        from kbase import feishu
        if not feishu.delete_credentials(sf):
            raise HTTPException(404, "未配置飞书凭据")
        return {"ok": True}

    @router.put("/settings/active-provider",
                dependencies=[deps.require_admin, deps.audit_mutation])
    def settings_set_active_provider(body: ActiveProviderBody):
        if providers_store.get_provider_dict(sf, body.name) is None:
            raise HTTPException(404, f"provider 不存在: {body.name}")
        providers_store.set_active(sf, body.name)
        return {"ok": True}

    # ---- 模型目录（M5-2 Provider UI：下拉选型号，免手抄）----

    def _resolve_refresh_credentials(body: ModelRefreshBody) -> tuple[str, str]:
        """解析 (base_url, api_key)。provider_name 优先用已存 provider 的
        base_url+密钥；否则用 body 的 base_url + api_key/api_key_env。
        缺凭据/缺 base_url 一律 422 给前端可读提示。"""
        if body.provider_name:
            p = providers_store.get_provider_dict(sf, body.provider_name)
            if p is None:
                raise HTTPException(404, f"provider 不存在: {body.provider_name}")
            key = p.get("api_key") or (os.environ.get(p["api_key_env"])
                                       if p["api_key_env"] else None)
            if not key:
                raise HTTPException(
                    422, f"provider {body.provider_name} 未配置可用密钥，无法拉取模型列表")
            return p["base_url"], key
        if not body.base_url.strip():
            raise HTTPException(422, "缺少 base_url")
        key = body.api_key or (os.environ.get(body.api_key_env)
                               if body.api_key_env else None)
        if not key:
            raise HTTPException(
                422, "请先填写 API Key（或有效的密钥环境变量名），再获取模型列表")
        return body.base_url.strip(), key

    @router.post("/settings/models/refresh",
                 dependencies=[deps.require_admin, deps.audit_mutation])
    def refresh_model_catalog(body: ModelRefreshBody):
        """手动拉取某端点的模型清单并写缓存。任何 OpenAI 兼容端点均可
        （含企业内部自有大模型平台，只要实现了 GET /models）。"""
        base_url, key = _resolve_refresh_credentials(body)
        try:
            models = model_catalog.fetch_models(base_url, key)
        except RuntimeError as e:
            raise HTTPException(502, str(e)) from e
        return model_catalog.save_catalog(sf, base_url, models)

    def _refresh_stale_quietly(base_url: str, key: str) -> None:
        """后台周更任务体：失败只记日志（下次访问再试），绝不影响请求方。"""
        try:
            model_catalog.save_catalog(
                sf, base_url, model_catalog.fetch_models(base_url, key))
        except RuntimeError as e:
            logger.warning("模型目录后台刷新失败（%s）: %s", base_url, e)

    @router.get("/settings/models", dependencies=[deps.require_admin])
    def list_model_catalogs(bg: BackgroundTasks):
        """读全部缓存目录。对 stale（>7天）且能从已配置 provider 找到凭据的
        base_url 自动挂后台刷新——"每周自动获取"由管理页访问驱动实现，
        不引入独立调度器。本次响应仍返回旧缓存（刷新结果下次读取可见）。"""
        catalogs = model_catalog.load_all(sf)
        providers = providers_store.list_providers(sf)
        for c in catalogs:
            if not c["stale"]:
                continue
            p = next((p for p in providers
                      if p["base_url"].rstrip("/") == c["base_url"]), None)
            if p is None:
                continue
            key = p.get("api_key") or (os.environ.get(p["api_key_env"])
                                       if p["api_key_env"] else None)
            if key:
                bg.add_task(_refresh_stale_quietly, c["base_url"], key)
        return {"catalogs": catalogs}

    @router.post("/settings/providers/{name}/test", dependencies=[deps.require_admin])
    async def settings_test_provider(name: str):
        if providers_store.get_provider_dict(sf, name) is None:
            raise HTTPException(404, f"provider 不存在: {name}")
        try:
            llm = svc.get_llm(name)
            start = time.perf_counter()
            await asyncio.wait_for(
                llm.complete([{"role": "user", "content": "回复：好"}]), timeout=10.0)
            latency_ms = (time.perf_counter() - start) * 1000
            return {"ok": True, "latency_ms": latency_ms}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "连通性测试超时(10s)"}
        except Exception as e:  # noqa: BLE001 —— 连通性探测，任何失败都回报而非 500
            return {"ok": False, "error": str(e)}
