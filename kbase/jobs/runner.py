"""JobRunner：顺序步骤编排。单个 job 内的步骤串行执行，每步执行前先把该步
标记为 running 并落库（供轮询端在执行期间就能看到进度），执行完成/失败
再更新为 done/failed，继续下一步——即便某步抛异常也不中断整体流程
（同摄取失败隔离哲学：单节失败不该拖垮整份文档）。

同步函数：由 FastAPI BackgroundTasks 承载（与摄取管道同模式），不引入
异步任务队列。
"""
import logging
from typing import Callable

from kbase.jobs.store import get_job, update_job

logger = logging.getLogger(__name__)

StepFn = Callable[[], str | None]


def run_job(sf, job_id: str, steps: list[tuple[str, StepFn]]) -> None:
    """按顺序执行 steps（(name, callable) 列表）。callable 签名 () -> str | None，
    返回值作为该步 detail；抛异常则该步标 failed，异常信息作 detail，继续
    执行后续步骤。

    整体状态（spec §3.3）：
    - 全部步骤成功 → done
    - 末步（产物写盘步）成功、但更早的步骤中有失败 → done_with_errors
    - 末步失败 → failed（没有可用产出）
    """
    update_job(sf, job_id, status="running")
    step_states: list[dict] = [{"name": name, "status": "pending", "detail": None}
                               for name, _ in steps]

    def _write_progress():
        update_job(sf, job_id, progress={"steps": step_states})

    any_failed = False
    for i, (name, fn) in enumerate(steps):
        step_states[i]["status"] = "running"
        _write_progress()
        try:
            detail = fn()
            step_states[i]["status"] = "done"
            step_states[i]["detail"] = detail
        except Exception as e:  # noqa: BLE001 —— 单步失败不阻断后续步骤
            logger.warning("job %s 步骤 %r 失败: %s", job_id, name, e)
            any_failed = True
            step_states[i]["status"] = "failed"
            step_states[i]["detail"] = str(e)
        _write_progress()

    last_ok = bool(steps) and step_states[-1]["status"] == "done"
    if not any_failed:
        final_status = "done"
    elif last_ok:
        final_status = "done_with_errors"
    else:
        final_status = "failed"

    error = None
    if final_status == "failed":
        failed_details = [s["detail"] for s in step_states if s["status"] == "failed"]
        error = "; ".join(failed_details) if failed_details else None

    update_job(sf, job_id, status=final_status, progress={"steps": step_states},
              error=error)
