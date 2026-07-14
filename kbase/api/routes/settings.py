"""Provider 设置域路由：LLM provider 的增删改查、默认切换与连通性测试。

providers 表以 DB 为唯一真源（首次启动由 YAML seed，见 services.py）；
任何 PUT/DELETE 后必须使 LLM 实例缓存失效（svc.invalidate_llm_cache），
下次 get_llm 按 DB 最新定义重建。"""
import asyncio
import time

from fastapi import HTTPException

from kbase import providers_store
from kbase.api.routes import RouteDeps
from kbase.api.schemas import ActiveProviderBody, ProviderCreate, ProviderUpdate
from kbase.api.services import Services


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf = svc.sf

    @router.get("/providers", dependencies=[deps.require_viewer])
    def providers():
        # 旧端点（Plan B 前旧前端仍用它）：改读 DB，返回结构保持不变
        return {"active": providers_store.get_active(sf),
                "providers": [p["name"] for p in providers_store.list_providers(sf)]}

    @router.get("/settings/providers", dependencies=[deps.require_admin])
    def settings_list_providers():
        return {"active": providers_store.get_active(sf),
                "providers": providers_store.list_providers(sf)}

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

    @router.put("/settings/active-provider",
                dependencies=[deps.require_admin, deps.audit_mutation])
    def settings_set_active_provider(body: ActiveProviderBody):
        if providers_store.get_provider_dict(sf, body.name) is None:
            raise HTTPException(404, f"provider 不存在: {body.name}")
        providers_store.set_active(sf, body.name)
        return {"ok": True}

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
