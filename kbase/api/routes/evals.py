"""评测回归域路由（B）：评测集 CRUD + 一键回归 + 历史对比 + 报告导出。

全部端点 editor 起步——评测是内容运营工具（建集/跑回归会占检索资源），
viewer 不开放；库可见性沿用 M6-3 ACL（无权库统一 404）。

T15 起 run 端点分两种 mode（见 run_eval_set）：
- retrieval（默认）：同步跑检索回归，秒级，**行为与改造前完全一致**；
- answer：建 eval_answer 任务后台跑（检索→生成→裁判），须开
  evals.answer_judge.enabled，关着就 422——不静默降级为 retrieval，
  否则调用方拿到 200 却只有检索指标，会以为答案分没跑出来是"结果就是空"。
"""
from pathlib import Path

from fastapi import BackgroundTasks, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from kbase import evals
from kbase import retrieval_strategy as rs
from kbase.api.guards import KbGuard
from kbase.api.routes import RouteDeps
from kbase.api.schemas import EvalRunBody, EvalSetCreate
from kbase.api.services import Services
from kbase.errors import AppError
from kbase.jobs.eval_answer import build_eval_answer_steps
from kbase.jobs.export_docx import markdown_to_docx
from kbase.jobs.runner import run_job
from kbase.jobs.store import create_job
from kbase.license import require_feature

_REPORT_MEDIA_TYPE = ("application/vnd.openxmlformats-officedocument"
                      ".wordprocessingml.document")


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf, cfg, retriever = svc.sf, svc.cfg, svc.retriever

    # T02/G09：评测集端点原先只判 ACL，没叠加 API Key 的库级 scope——
    # 受限 key 能读写白名单外库的评测集（评测集会跑检索、暴露库内容形状）。
    # 统一收敛到 KbGuard（ACL 且 scope）。
    guard = KbGuard(sf)

    @router.post("/kb/{kb_id}/eval-sets",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def create_eval_set(kb_id: str, body: EvalSetCreate, request: Request):
        guard.kb(kb_id, request)
        return evals.create_set(sf, kb_id, body.name,
                                [c.model_dump(exclude_none=True) for c in body.cases])

    @router.get("/kb/{kb_id}/eval-sets", dependencies=[deps.require_editor])
    def list_eval_sets(kb_id: str, request: Request):
        guard.kb(kb_id, request)
        return evals.list_sets(sf, kb_id)

    @router.delete("/eval-sets/{set_id}",
                   dependencies=[deps.require_editor, deps.audit_mutation])
    def delete_eval_set(set_id: str, request: Request):
        row = evals.get_set(sf, set_id)
        if row is None:
            raise AppError("error.eval_set_not_found", "评测集不存在: {id}", status=404, id=set_id)
        guard.kb(row.kb_id, request)
        evals.delete_set(sf, set_id)
        return {"ok": True}

    @router.post("/eval-sets/{set_id}/run",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    async def run_eval_set(set_id: str, body: EvalRunBody, request: Request,
                           bg: BackgroundTasks):
        """一键回归。

        mode="retrieval"（默认）：按该库当前检索策略跑（与线上问答同路径），
        逐用例检索是同步 CPU/IO 混合操作，整体进线程池避免阻塞事件循环。
        这条路径不碰任何 LLM，行为与 T15 之前逐字节一致。

        mode="answer"：检索→生成→裁判，每条用例两次 LLM 调用、分钟级耗时，
        绝不能在这里同步等——建 job 立刻返回 id，判分交给 BackgroundTasks
        （调用方按 /api/jobs/{id} 轮询进度，产物 artifact.md 即报告）。
        """
        row = evals.get_set(sf, set_id)
        if row is None:
            raise AppError("error.eval_set_not_found", "评测集不存在: {id}", status=404, id=set_id)
        guard.kb(row.kb_id, request)

        if body.mode != evals.MODE_ANSWER:
            strategy = rs.resolve_strategy(cfg, rs.kb_retrieval_config(sf, row.kb_id))
            result = await run_in_threadpool(
                evals.run_eval, sf, retriever, set_id,
                top_k=body.top_k, strategy=strategy)
            return result

        if not cfg.evals.answer_judge.enabled:
            raise AppError("error.answer_judge_disabled",
                           "答案级评测未启用（需在配置里打开 "
                           "evals.answer_judge.enabled）", status=422)
        # T16 功能位：答案级评测（T15 的 LLM-judge 路径）属于 answer_eval。
        # 拦在这里而不是做成路由级依赖——同一端点还承载 retrieval 模式，
        # 那条路径不含 LLM 判分（老客户照用），只能按 mode 分支判定。
        require_feature("answer_eval")()
        judge_provider = cfg.evals.answer_judge.provider
        # T15 spec §2：建 job 并**立刻**返回 job id，判分不在请求里同步跑。
        # job 先落库、任务体整段挂到 BackgroundTasks（与 /api/jobs 同一模式），
        # 所以请求路径上只有一次 DB 写 + 一次入队——不含任何检索/LLM 调用。
        job = create_job(sf, kb_id=row.kb_id, type="eval_answer",
                         params={"set_id": set_id, "top_k": body.top_k,
                                 "judge_provider": judge_provider},
                         provider=body.provider or judge_provider)

        def _run_eval_answer_job() -> None:
            strategy = rs.resolve_strategy(cfg, rs.kb_retrieval_config(sf, row.kb_id))
            steps = build_eval_answer_steps(
                sf, retriever,
                # 生成走问答同一条链路（显式请求 > 活跃 provider）；
                # 裁判固定用另配的便宜 provider（None=活跃 provider）。
                gen_llm=svc.get_llm(body.provider),
                judge_llm=svc.get_llm(judge_provider),
                set_id=set_id, kb_id=row.kb_id, top_k=body.top_k,
                strategy=strategy,
                min_score=rs.pick_min_score(cfg, strategy, retriever.rerank_active),
                min_include_score=cfg.retrieval.min_include_score,
                judge_provider=judge_provider, job_id=job["id"],
                jobs_dir=cfg.data_dir / "jobs")
            run_job(sf, job["id"], steps)

        bg.add_task(_run_eval_answer_job)
        return {"id": job["id"], "mode": evals.MODE_ANSWER, "status": "pending"}

    @router.get("/eval-sets/{set_id}/runs", dependencies=[deps.require_editor])
    def list_eval_runs(set_id: str, request: Request):
        row = evals.get_set(sf, set_id)
        if row is None:
            raise AppError("error.eval_set_not_found", "评测集不存在: {id}", status=404, id=set_id)
        guard.kb(row.kb_id, request)
        return evals.list_runs(sf, set_id)

    @router.get("/eval-runs/{run_id}", dependencies=[deps.require_editor])
    def get_eval_run(run_id: str, request: Request):
        run = evals.get_run(sf, run_id)
        if run is None:
            raise AppError("error.eval_run_not_found", "回归记录不存在: {id}", status=404, id=run_id)
        row = evals.get_set(sf, run["set_id"])
        if row is not None:
            guard.kb(row.kb_id, request)
        return run

    @router.get("/eval-runs/{run_id}/report", dependencies=[deps.require_editor])
    def get_eval_run_report(run_id: str, request: Request, format: str = "md"):
        """回归报告导出（T15 spec §4）：md 直接返回，docx 按需转换后返回。

        报告内容由 kbase.evals.render_report 渲染（与 eval_answer 任务的
        artifact.md 同一份渲染器），含用例数、检索指标、答案分、裁判模型与
        复测口径——只给一个总分不算报告。
        """
        record = evals.get_run_record(sf, run_id)
        if record is None:
            raise AppError("error.eval_run_not_found", "回归记录不存在: {id}",
                           status=404, id=run_id)
        row = evals.get_set(sf, record["set_id"])
        if row is not None:
            guard.kb(row.kb_id, request)

        fmt = (format or "md").lower()
        if fmt not in ("md", "docx"):
            raise AppError("error.unsupported_report_format",
                           "不支持的报告格式: {format}", status=422, format=format)
        md = evals.render_report(record)
        if fmt == "md":
            out_dir = _report_dir(cfg, run_id)
            out_path = out_dir / "report.md"
            out_path.write_text(md, encoding="utf-8")
            return FileResponse(out_path, media_type="text/markdown",
                                filename="eval-report.md")

        out_dir = _report_dir(cfg, run_id)
        docx_path = out_dir / "report.docx"
        # T16 功能位：docx 是"可交付的成品报告导出"，归 bundle_export；
        # md 是同一份内容的在线预览，不拦（否则连报告都看不了）。
        require_feature("bundle_export")()
        markdown_to_docx(md, docx_path)
        return FileResponse(docx_path, media_type=_REPORT_MEDIA_TYPE,
                            filename="评测报告.docx")


def _report_dir(cfg, run_id: str) -> Path:
    """报告落盘目录：{data_dir}/reports/{run_id}/。按需生成（同一 run 反复
    下载覆盖同一份文件即可，报告是 run 的纯函数，没有增量语义）。"""
    out_dir = cfg.data_dir / "reports" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir
