from kbase.jobs.runner import run_job
from kbase.jobs.store import create_job, get_job


def _sf(tmp_path):
    from kbase.db import make_session_factory
    return make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")


def test_all_steps_succeed_status_done(tmp_path):
    sf = _sf(tmp_path)
    job = create_job(sf, kb_id="kb1", type="proposal", params={}, provider=None)
    calls = []

    def step_a():
        calls.append("a")
        return "步骤A完成"

    def step_b():
        calls.append("b")
        return None

    run_job(sf, job["id"], [("步骤A", step_a), ("步骤B", step_b)])

    assert calls == ["a", "b"]
    got = get_job(sf, job["id"])
    assert got["status"] == "done"
    steps = got["progress"]["steps"]
    assert steps == [
        {"name": "步骤A", "status": "done", "detail": "步骤A完成"},
        {"name": "步骤B", "status": "done", "detail": None},
    ]


def test_middle_step_fails_continues_and_done_with_errors(tmp_path):
    """中间步骤失败：该步标 failed 并记 detail，后续步骤继续执行；
    末步（产物写盘）成功 → done_with_errors。"""
    sf = _sf(tmp_path)
    job = create_job(sf, kb_id="kb1", type="proposal", params={}, provider=None)
    calls = []

    def step_a():
        calls.append("a")
        return None

    def step_fail():
        calls.append("fail")
        raise ValueError("检索超时")

    def step_last():
        calls.append("last")
        return "产物已写盘"

    run_job(sf, job["id"], [("检索", step_a), ("生成节", step_fail),
                            ("写产物", step_last)])

    assert calls == ["a", "fail", "last"]     # 继续执行后续步骤
    got = get_job(sf, job["id"])
    assert got["status"] == "done_with_errors"
    steps = got["progress"]["steps"]
    assert steps[0] == {"name": "检索", "status": "done", "detail": None}
    assert steps[1]["status"] == "failed"
    assert "检索超时" in steps[1]["detail"]
    assert steps[2] == {"name": "写产物", "status": "done", "detail": "产物已写盘"}


def test_last_step_fails_status_failed(tmp_path):
    """末步（产物写盘）失败 → 无成功产出，整体 failed，即便更早的步骤成功。"""
    sf = _sf(tmp_path)
    job = create_job(sf, kb_id="kb1", type="proposal", params={}, provider=None)

    def step_ok():
        return None

    def step_last_fail():
        raise RuntimeError("磁盘写入失败")

    run_job(sf, job["id"], [("检索", step_ok), ("写产物", step_last_fail)])

    got = get_job(sf, job["id"])
    assert got["status"] == "failed"
    steps = got["progress"]["steps"]
    assert steps[0]["status"] == "done"
    assert steps[1]["status"] == "failed"
    assert "磁盘写入失败" in steps[1]["detail"]
    assert got["error"]
    assert "磁盘写入失败" in got["error"]


def test_progress_written_incrementally_before_completion(tmp_path):
    """每步执行前先写 running 态进度，验证逐步写入（不是全部跑完才写一次）。"""
    sf = _sf(tmp_path)
    job = create_job(sf, kb_id="kb1", type="proposal", params={}, provider=None)
    snapshots = []

    def step_a():
        # 执行期间读一次 job，应已被标记为 running
        snapshots.append(get_job(sf, job["id"])["progress"]["steps"][0]["status"])
        return None

    def step_b():
        snapshots.append(get_job(sf, job["id"])["progress"]["steps"][1]["status"])
        return None

    run_job(sf, job["id"], [("A", step_a), ("B", step_b)])

    assert snapshots == ["running", "running"]
    got = get_job(sf, job["id"])
    assert got["status"] == "done"


def test_job_status_running_during_execution(tmp_path):
    sf = _sf(tmp_path)
    job = create_job(sf, kb_id="kb1", type="proposal", params={}, provider=None)
    seen_status = []

    def step_a():
        seen_status.append(get_job(sf, job["id"])["status"])
        return None

    run_job(sf, job["id"], [("A", step_a)])
    assert seen_status == ["running"]
