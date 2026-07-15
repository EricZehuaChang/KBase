"""评测回归域路由（B）：评测集 CRUD + 一键回归 + 历史对比。

全部端点 editor 起步——评测是内容运营工具（建集/跑回归会占检索资源），
viewer 不开放；库可见性沿用 M6-3 ACL（无权库统一 404）。
"""
from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from kbase import evals, kb_acl
from kbase import retrieval_strategy as rs
from kbase.api.routes import RouteDeps
from kbase.api.schemas import EvalRunBody, EvalSetCreate
from kbase.api.services import Services
from kbase.models import KnowledgeBase


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf, cfg, retriever = svc.sf, svc.cfg, svc.retriever

    def _guard_kb(kb_id: str, request: Request) -> None:
        actor = getattr(request.state, "actor", None) or {"role": "admin"}
        with sf() as s:
            exists = s.get(KnowledgeBase, kb_id) is not None
        if not exists or not kb_acl.can_access(sf, kb_id, actor):
            raise HTTPException(404, f"知识库不存在: {kb_id}")

    @router.post("/kb/{kb_id}/eval-sets",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def create_eval_set(kb_id: str, body: EvalSetCreate, request: Request):
        _guard_kb(kb_id, request)
        return evals.create_set(sf, kb_id, body.name,
                                [c.model_dump(exclude_none=True) for c in body.cases])

    @router.get("/kb/{kb_id}/eval-sets", dependencies=[deps.require_editor])
    def list_eval_sets(kb_id: str, request: Request):
        _guard_kb(kb_id, request)
        return evals.list_sets(sf, kb_id)

    @router.delete("/eval-sets/{set_id}",
                   dependencies=[deps.require_editor, deps.audit_mutation])
    def delete_eval_set(set_id: str, request: Request):
        row = evals.get_set(sf, set_id)
        if row is None:
            raise HTTPException(404, f"评测集不存在: {set_id}")
        _guard_kb(row.kb_id, request)
        evals.delete_set(sf, set_id)
        return {"ok": True}

    @router.post("/eval-sets/{set_id}/run",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    async def run_eval_set(set_id: str, body: EvalRunBody, request: Request):
        """一键回归：按该库当前检索策略跑（与线上问答同路径），逐用例检索
        是同步 CPU/IO 混合操作，整体进线程池避免阻塞事件循环。"""
        row = evals.get_set(sf, set_id)
        if row is None:
            raise HTTPException(404, f"评测集不存在: {set_id}")
        _guard_kb(row.kb_id, request)
        strategy = rs.resolve_strategy(cfg, rs.kb_retrieval_config(sf, row.kb_id))
        result = await run_in_threadpool(
            evals.run_eval, sf, retriever, set_id,
            top_k=body.top_k, strategy=strategy)
        return result

    @router.get("/eval-sets/{set_id}/runs", dependencies=[deps.require_editor])
    def list_eval_runs(set_id: str, request: Request):
        row = evals.get_set(sf, set_id)
        if row is None:
            raise HTTPException(404, f"评测集不存在: {set_id}")
        _guard_kb(row.kb_id, request)
        return evals.list_runs(sf, set_id)

    @router.get("/eval-runs/{run_id}", dependencies=[deps.require_editor])
    def get_eval_run(run_id: str, request: Request):
        run = evals.get_run(sf, run_id)
        if run is None:
            raise HTTPException(404, f"回归记录不存在: {run_id}")
        row = evals.get_set(sf, run["set_id"])
        if row is not None:
            _guard_kb(row.kb_id, request)
        return run
