"""生成任务域路由：方案大纲、长任务（方案/汇编）的创建、查询与产物下载。"""
from pathlib import Path

from fastapi import BackgroundTasks, HTTPException
from fastapi.responses import FileResponse

from kbase.api.routes import RouteDeps
from kbase.api.schemas import JobCreate, OutlineBody
from kbase.api.services import Services
from kbase.jobs.digest import build_digest_steps
from kbase.jobs.export_docx import markdown_to_docx
from kbase.jobs.proposal import build_proposal_steps, generate_outline
from kbase.jobs.runner import run_job
from kbase.jobs.store import create_job, get_job, list_jobs
from kbase.models import KnowledgeBase

_ARTIFACT_FILENAME = {"proposal": "方案.docx", "digest": "汇编.docx"}
_DOCX_MEDIA_TYPE = ("application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document")


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf, cfg, retriever = svc.sf, svc.cfg, svc.retriever

    @router.post("/proposals/outline", dependencies=[deps.require_editor, deps.audit_mutation])
    async def proposals_outline(body: OutlineBody):
        try:
            llm = svc.get_llm(body.provider)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(503, str(e)) from e
        try:
            return await generate_outline(retriever, llm, body.kb_id, body.topic,
                                          body.requirements)
        except ValueError as e:
            raise HTTPException(502, str(e)) from e

    @router.post("/jobs", dependencies=[deps.require_editor, deps.audit_mutation])
    def create_job_endpoint(body: JobCreate, bg: BackgroundTasks):
        if body.type not in ("proposal", "digest"):
            raise HTTPException(422, f"未知的 job type: {body.type}")
        if body.type == "proposal":
            if "topic" not in body.params or "outline" not in body.params:
                raise HTTPException(422, "proposal job 缺少必需参数：topic/outline")
        with sf() as s:
            kb = s.get(KnowledgeBase, body.kb_id)
            if kb is None:
                raise HTTPException(404, f"知识库不存在: {body.kb_id}")
            kb_name = kb.name
        try:
            llm = svc.get_llm(body.provider)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(503, str(e)) from e

        job = create_job(sf, kb_id=body.kb_id, type=body.type,
                         params=body.params, provider=body.provider)
        jobs_dir = cfg.data_dir / "jobs"

        if body.type == "proposal":
            steps = build_proposal_steps(
                sf, retriever, llm, kb_id=body.kb_id,
                topic=body.params["topic"], outline=body.params["outline"],
                job_id=job["id"], jobs_dir=jobs_dir)
        else:
            steps = build_digest_steps(
                sf, llm, kb_id=body.kb_id, doc_ids=body.params.get("doc_ids"),
                job_id=job["id"], files_dir=cfg.data_dir / "files",
                jobs_dir=jobs_dir, kb_name=kb_name)

        bg.add_task(run_job, sf, job["id"], steps)
        return {"id": job["id"]}

    @router.get("/jobs", dependencies=[deps.require_viewer])
    def jobs_list(kb_id: str):
        return list_jobs(sf, kb_id)

    @router.get("/jobs/{job_id}", dependencies=[deps.require_viewer])
    def jobs_detail(job_id: str):
        job = get_job(sf, job_id)
        if job is None:
            raise HTTPException(404, f"job 不存在: {job_id}")
        return job

    @router.get("/jobs/{job_id}/artifact", dependencies=[deps.require_viewer])
    def jobs_artifact(job_id: str, format: str = "md"):
        job = get_job(sf, job_id)
        if job is None:
            raise HTTPException(404, f"job 不存在: {job_id}")
        if job["status"] not in ("done", "done_with_errors"):
            raise HTTPException(409, f"job 尚未完成: status={job['status']}")
        if not job["artifact_path"]:
            raise HTTPException(404, "产物不存在")
        md_path = Path(job["artifact_path"])
        if not md_path.exists():
            raise HTTPException(404, "产物文件不存在")

        if format == "md":
            return FileResponse(md_path, media_type="text/markdown",
                                filename="artifact.md")

        # docx：首次请求时按需转换并缓存在 md 旁边
        docx_path = md_path.with_suffix(".docx")
        if not docx_path.exists():
            markdown_to_docx(md_path.read_text(encoding="utf-8"), docx_path)
        download_name = _ARTIFACT_FILENAME.get(job["type"], "artifact.docx")
        return FileResponse(docx_path, media_type=_DOCX_MEDIA_TYPE,
                            filename=download_name)
