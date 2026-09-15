"""答案级评测任务（T15）：逐用例 检索 → 生成 → 裁判打分，最后落一条
eval_runs 快照（同时带 hit@k/MRR 与答案分）。

为什么是 job 而不是同步端点：答案级判分每条用例至少两次 LLM 调用
（生成 + 裁判），一个 30 条用例的集就是分钟级耗时，同步跑会把 API 进程的
请求槽位占死（原检索回归端点秒级，同步跑没问题）。走既有 jobs 通道
（BackgroundTasks + runner.py 步骤模型）后，创建请求立刻返回 job id，
前端按 useJob 轮询进度。

步骤切分（与 build_proposal_steps / build_digest_steps 同模式）：
- 每条用例一步："用例：{question}"，步内 asyncio.run 串起
  检索（retriever，与线上问答同一路径）→ 生成（Generator + usable_blocks，
  与 _run_query 同一套组件与拒答语义）→ 裁判（另配的便宜 provider）。
- 末步："落快照"，写 eval_runs（含逐用例明细）并渲染 artifact.md、
  update_job(artifact_path=...)。

判分失败的隔离有两层，**都必要**：
- 检索或生成抛异常 → 该步上抛，runner.py 把它标 failed 并继续后续用例
  （一条用例炸掉不该让整份回归作废），该用例不进任何分母。
- 裁判单独再包一层 try/except：它失败时**不能**上抛——上抛会让 runner 丢掉
  这一步已经拿到的生成答案（运营最想看的恰恰是"这条答成了这样、裁判没判
  出来"）。所以裁判失败在用例结果里记 judge_error，答案分分母排除它，
  该用例的检索指标照常计入。
"""
import asyncio
import json
from pathlib import Path
from typing import Callable

from kbase.evals import (ANSWER_JUDGE_SYSTEM_PROMPT, ANSWER_JUDGE_TEMPLATE,
                         MODE_ANSWER, _persist_run, build_run_record,
                         grade_answer, render_report, retrieval_facts)
from kbase.jobs.store import update_job
from kbase.models import EvalSet
from kbase.rag.generator import Generator

# 裁判 prompt 里两段文本的上限：参考答案是运营手写的（通常一两句，2000 字
# 足够）；模型答案来自生成链路，截 3000 字避免超长答案把 prompt 顶爆——
# 评的是"答没答到点上"，不是"答得完不完整"，超长部分对判分没有增量信息。
JUDGE_REFERENCE_CHARS = 2000
JUDGE_ANSWER_CHARS = 3000

# 未给参考答案的用例：无从判分（不是判错），单独一个标记，便于报告区分
# "没参考答案"与"裁判调用失败"。
ERR_NO_REFERENCE = "no_reference"
ERR_JUDGE_FAILED = "judge_failed"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def judge_messages(question: str, reference: str, answer: str) -> list[dict]:
    """构造裁判请求消息（固定 0~1 rubric，文案见 evals.ANSWER_JUDGE_SYSTEM_PROMPT）。

    抽成纯函数便于测试直接断言 rubric 与"参考答案/模型答案确实进了 prompt"
    ——断言不需要任何 LLM。
    """
    return [
        {"role": "system", "content": ANSWER_JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": ANSWER_JUDGE_TEMPLATE.format(
            question=question,
            reference=_truncate(reference, JUDGE_REFERENCE_CHARS),
            answer=_truncate(answer, JUDGE_ANSWER_CHARS))},
    ]


async def _collect(stream) -> str:
    """把 answer_stream 收成整段文本。仍走流式接口（而非 complete）：与
    /api/kb/{id}/query 是**同一条生成路径**——拒答短路、prompt 组装、usable
    门都在 Generator 里，评测要评的就是线上这一份。"""
    pieces = []
    async for piece in stream:
        pieces.append(piece)
    return "".join(pieces)


def build_eval_answer_steps(sf, retriever, gen_llm, judge_llm, *, set_id: str,
                            kb_id: str, top_k: int, strategy, min_score: float,
                            min_include_score: float, judge_provider: str | None,
                            job_id: str, jobs_dir) -> list[tuple[str, Callable]]:
    """为 runner 生成步骤列表：逐用例一步 + 落快照一步。

    各步内部用 asyncio.run 包一层 async 调用（同 build_proposal_steps /
    build_digest_steps 的模式——run_job 由 BackgroundTasks 工作线程调用，
    线程内无运行中的事件循环，可以直接 asyncio.run）。

    逐用例结果按评测集顺序收进闭包，落快照步一次性汇整：hit@k/MRR 的口径
    只在 kbase.evals 里算一份（build_run_record → aggregate_retrieval），
    判分侧不复刻第二份。
    """
    with sf() as s:
        row = s.get(EvalSet, set_id)
        if row is None:
            raise ValueError(f"评测集不存在: {set_id}")
        cases = json.loads(row.cases)
    jobs_dir = Path(jobs_dir)

    inferences: list[dict | None] = [None] * len(cases)
    answers: dict[int, str] = {}
    judges: dict[int, dict] = {}
    case_errors: dict[int, str] = {}

    def _judge_sync(question: str, reference: str, answer: str):
        async def _call():
            raw = await judge_llm.complete(judge_messages(question, reference, answer))
            return grade_answer(raw)
        return asyncio.run(_call())

    def _make_case_step(index: int, case: dict) -> Callable:
        question = case["question"]

        def step():
            # 检索或生成失败的用例：单独记下原因（明细里标 case_error），
            # 并把异常继续上抛让 runner 标记该步 failed —— 但**不放任它把
            # 整批作废**：落快照步能容忍缺数据的用例，所以 30 条里坏 1 条时
            # 其余 29 条的指标与答案分照常产出（整体 done_with_errors）。
            # 若让异常直接冒到快照步，最后一步也会失败 → 一条 run 都不落，
            # 运营白等几分钟还拿不到任何可比基线。
            try:
                blocks = retriever.retrieve(kb_id, question, top_k, False, strategy)
                inferences[index] = retrieval_facts(case, blocks)
                gen = Generator(gen_llm, min_score=min_score,
                                min_include_score=min_include_score)
                usable = gen.usable_blocks(blocks)
                answer = asyncio.run(_collect(gen.answer_stream(question, usable)))
            except Exception as e:  # noqa: BLE001 —— 单条用例失败不阻断整批
                case_errors[index] = f"检索或生成失败：{e}"
                raise
            answers[index] = answer

            reference = (case.get("expected_answer") or "").strip()
            if not reference:
                judges[index] = {"score": None, "judge_provider": judge_provider,
                                 "error": ERR_NO_REFERENCE,
                                 "reason": "用例未给参考答案（expected_answer），无法判分"}
                return "未判分（无参考答案）"
            try:
                score, reason = _judge_sync(question, reference, answer)
                judges[index] = {"score": score, "reason": reason,
                                 "judge_provider": judge_provider, "error": None}
                return f"{score:.2f} · {reason}"
            except Exception as e:  # noqa: BLE001 —— 单条判分失败不阻断整批
                judges[index] = {"score": None, "judge_provider": judge_provider,
                                 "error": ERR_JUDGE_FAILED,
                                 "reason": f"判分失败：{e}"}
                return f"判分失败：{e}"

        return step

    def _write_snapshot_step():
        record = build_run_record(cases, inferences, set_id=set_id, top_k=top_k,
                                  mode=MODE_ANSWER, answers=answers,
                                  judges=judges, judge_provider=judge_provider,
                                  case_errors=case_errors)
        _persist_run(sf, record)

        out_dir = jobs_dir / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "artifact.md"
        out_path.write_text(render_report(record), encoding="utf-8")
        update_job(sf, job_id, artifact_path=str(out_path))
        return str(out_path)

    steps: list[tuple[str, Callable]] = [
        (f"用例：{case['question']}", _make_case_step(i, case))
        for i, case in enumerate(cases)
    ]
    steps.append(("落快照", _write_snapshot_step))
    return steps
