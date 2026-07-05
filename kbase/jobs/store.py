"""Jobs 存取层：CRUD + JSON 字段编解码。HTTP 编排在 api/main.py，执行编排在 runner.py。"""
import json
import uuid
from datetime import datetime

from kbase.models import Job


def _to_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "kb_id": job.kb_id,
        "type": job.type,
        "status": job.status,
        "params": json.loads(job.params) if job.params is not None else None,
        "progress": json.loads(job.progress) if job.progress is not None else None,
        "artifact_path": job.artifact_path,
        "error": job.error,
        "provider": job.provider,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def create_job(sf, kb_id: str, type: str, params: dict,
                provider: str | None = None) -> dict:
    job = Job(id=str(uuid.uuid4()), kb_id=kb_id, type=type,
              params=json.dumps(params, ensure_ascii=False), provider=provider)
    with sf() as s:
        s.add(job)
        s.commit()
        s.refresh(job)
        return _to_dict(job)


def get_job(sf, id: str) -> dict | None:
    with sf() as s:
        job = s.get(Job, id)
        if job is None:
            return None
        return _to_dict(job)


def list_jobs(sf, kb_id: str, limit: int = 20) -> list[dict]:
    with sf() as s:
        rows = (s.query(Job).filter_by(kb_id=kb_id)
                .order_by(Job.updated_at.desc()).limit(limit).all())
        return [_to_dict(j) for j in rows]


def update_job(sf, id: str, **fields) -> dict | None:
    """更新指定字段；progress 若传入 dict 自动 json.dumps；updated_at 每次调用刷新。
    支持字段：status/progress/artifact_path/error。"""
    with sf() as s:
        job = s.get(Job, id)
        if job is None:
            return None
        if "status" in fields:
            job.status = fields["status"]
        if "progress" in fields:
            progress = fields["progress"]
            job.progress = (json.dumps(progress, ensure_ascii=False)
                            if progress is not None else None)
        if "artifact_path" in fields:
            job.artifact_path = fields["artifact_path"]
        if "error" in fields:
            job.error = fields["error"]
        job.updated_at = datetime.utcnow()
        s.commit()
        s.refresh(job)
        return _to_dict(job)
