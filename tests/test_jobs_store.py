import json

from kbase.db import make_session_factory
from kbase.jobs.store import create_job, get_job, list_jobs, update_job


def _sf(tmp_path):
    return make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")


def test_create_and_get_job_roundtrip(tmp_path):
    sf = _sf(tmp_path)
    job = create_job(sf, kb_id="kb1", type="proposal",
                      params={"topic": "住房保障"}, provider="qwen-plus")
    assert job["kb_id"] == "kb1"
    assert job["type"] == "proposal"
    assert job["status"] == "pending"
    assert job["params"] == {"topic": "住房保障"}
    assert job["provider"] == "qwen-plus"
    assert job["id"]

    got = get_job(sf, job["id"])
    assert got["id"] == job["id"]
    assert got["params"] == {"topic": "住房保障"}
    assert got["progress"] is None
    assert got["artifact_path"] is None
    assert got["error"] is None


def test_get_unknown_job_returns_none(tmp_path):
    sf = _sf(tmp_path)
    assert get_job(sf, "nope") is None


def test_list_jobs_orders_by_updated_at_desc(tmp_path):
    sf = _sf(tmp_path)
    j1 = create_job(sf, kb_id="kb1", type="proposal", params={}, provider=None)
    j2 = create_job(sf, kb_id="kb1", type="digest", params={}, provider=None)
    # touch j1 so it becomes most-recently updated
    update_job(sf, j1["id"], status="running")
    items = list_jobs(sf, "kb1")
    assert [i["id"] for i in items] == [j1["id"], j2["id"]]


def test_list_jobs_filters_by_kb_id(tmp_path):
    sf = _sf(tmp_path)
    create_job(sf, kb_id="kb1", type="proposal", params={}, provider=None)
    create_job(sf, kb_id="kb2", type="proposal", params={}, provider=None)
    items = list_jobs(sf, "kb1")
    assert len(items) == 1
    assert items[0]["kb_id"] == "kb1"


def test_update_job_progress_json_roundtrip(tmp_path):
    sf = _sf(tmp_path)
    job = create_job(sf, kb_id="kb1", type="proposal", params={}, provider=None)
    progress = {"steps": [{"name": "大纲", "status": "done", "detail": None}]}
    update_job(sf, job["id"], status="running", progress=progress)
    got = get_job(sf, job["id"])
    assert got["status"] == "running"
    assert got["progress"] == progress


def test_update_job_refreshes_updated_at(tmp_path):
    sf = _sf(tmp_path)
    job = create_job(sf, kb_id="kb1", type="proposal", params={}, provider=None)
    before = get_job(sf, job["id"])["updated_at"]
    update_job(sf, job["id"], status="done", artifact_path="data/jobs/1/artifact.md")
    after = get_job(sf, job["id"])
    assert after["updated_at"] >= before
    assert after["artifact_path"] == "data/jobs/1/artifact.md"
    assert after["status"] == "done"


def test_update_job_error_field(tmp_path):
    sf = _sf(tmp_path)
    job = create_job(sf, kb_id="kb1", type="proposal", params={}, provider=None)
    update_job(sf, job["id"], status="failed", error="boom")
    got = get_job(sf, job["id"])
    assert got["status"] == "failed"
    assert got["error"] == "boom"
