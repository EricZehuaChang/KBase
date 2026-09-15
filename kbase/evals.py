"""检索评测回归（B）：评测集 CRUD + 一键回归（hit@k / MRR）+ 历史对比。

设计取舍：
- **默认只评检索，不评生成**——检索指标确定、免 LLM 费用、秒级出结果；
  生成质量依赖模型且难自动判分，留给人工抽查（运营看板已有拒答清单）。
- **答案级评测（T15）可选且默认关闭**（`evals.answer_judge.enabled=false`）：
  开启后才走"检索→生成→另配的便宜模型裁判打分"这条链路。为什么默认关：
  答案级判分按用例数计费、耗时以分钟计，而且它评的是**当时的生成配置**而不是
  检索配置——升级动作本身不该悄悄打开一个会花钱、会改变回归曲线含义的口径。
  关着的时候本模块的代码路径与本卡之前逐字节一致，一个 LLM 调用都不发。
- 用例判中规则：expect_doc（命中块所属文档名相等）或 expect_text
  （期望文本是命中块正文的子串），二者有其一即可、都给则任一命中算中。
- hit@k = 命中用例数/总数；MRR = 平均(1/首个命中块的名次)，未命中记 0。
- 每次回归落一行 eval_runs 快照（含逐用例明细），调参前后各跑一次对比
  hit/mrr 即回答"这次改配置是变好还是变坏"。答案级回归在同一行里另存
  answer_score / judge_provider（mode 区分口径），因此历史清单能同时看到
  "检索变没变好"与"答案分变没变好"，而不是两个互不相干的数。
"""
import json
import re
import uuid

from kbase.models import EvalRun, EvalSet

MODE_RETRIEVAL = "retrieval"
MODE_ANSWER = "answer"


def create_set(sf, kb_id: str, name: str, cases: list[dict]) -> dict:
    """建评测集。cases 每条至少要有 question 和 expect_doc/expect_text 之一
    （schema 层已校验，这里直接存）。"""
    row = EvalSet(id=str(uuid.uuid4()), kb_id=kb_id, name=name,
                  cases=json.dumps(cases, ensure_ascii=False))
    with sf() as s:
        s.add(row)
        s.commit()
        return {"id": row.id, "kb_id": kb_id, "name": name,
                "case_count": len(cases)}


def list_sets(sf, kb_id: str) -> list[dict]:
    with sf() as s:
        rows = (s.query(EvalSet).filter(EvalSet.kb_id == kb_id)
                .order_by(EvalSet.created_at.desc()).all())
        return [{"id": r.id, "name": r.name,
                 "case_count": len(json.loads(r.cases)),
                 "created_at": r.created_at.isoformat()} for r in rows]


def get_set(sf, set_id: str) -> EvalSet | None:
    with sf() as s:
        return s.get(EvalSet, set_id)


def delete_set(sf, set_id: str) -> bool:
    with sf() as s:
        row = s.get(EvalSet, set_id)
        if row is None:
            return False
        s.query(EvalRun).filter(EvalRun.set_id == set_id).delete()
        s.delete(row)
        s.commit()
        return True


def _case_hit_rank(case: dict, blocks) -> int | None:
    """返回该用例首个命中块的名次（1 起），未命中返回 None。"""
    expect_doc = case.get("expect_doc")
    expect_text = case.get("expect_text")
    for rank, b in enumerate(blocks, start=1):
        if expect_doc and b.doc_name == expect_doc:
            return rank
        if expect_text and expect_text in b.text:
            return rank
    return None


def _retrieval_judgeable(case: dict) -> bool:
    """该用例能否被**检索**判分（即给了 expect_doc / expect_text）。

    T13：标问回灌的用例还带 expected_answer（参考答案，供答案级判分 T15），
    它不是检索期望——只带它的用例检索侧无从判中，不能算进 hit@k/MRR 的
    分母，否则"回灌一条答案级用例"就会凭空空拉低 hit 率、把回归曲线带偏。
    """
    return bool(case.get("expect_doc") or case.get("expect_text"))


def retrieval_facts(case: dict, blocks) -> dict:
    """单用例的检索侧事实（不算总分）：是否参与检索判分、命中名次、top1 文档。

    抽成公开函数是为了让答案级评测（jobs/eval_answer.py）复用**同一份**
    "什么算检索判中"的口径——两处各写一遍迟早会漂移。
    """
    judgeable = _retrieval_judgeable(case)
    return {"rank": _case_hit_rank(case, blocks) if judgeable else None,
            "retrieval_judged": judgeable,
            "top_doc": (blocks[0].doc_name if blocks else None)}


def infer_retrieval(retriever, kb_id: str, cases: list[dict], *, top_k: int,
                    strategy=None) -> list[dict]:
    """逐用例检索，返回与 cases 等长的 retrieval_facts 列表。

    每题都检索（答案级判分 T15 要用同一批命中块），但 hit@k/MRR 只统计有
    检索期望的用例——见 _retrieval_judgeable。
    """
    return [retrieval_facts(case, retriever.retrieve(kb_id, case["question"],
                                                     top_k, False, strategy))
            for case in cases]


def aggregate_retrieval(inferences: list[dict]) -> dict:
    """把逐用例检索事实汇成 hit@k/MRR。

    `total` = 参与检索判分的用例数；未被检索判分的用例数另给 `skipped`——
    两者都报出来，口径变化在结果里可见。分母为 0 时指标记 0.0（不抛除零）。
    """
    hits = sum(1 for i in inferences if i["rank"] is not None)
    rr_sum = sum(1.0 / i["rank"] for i in inferences if i["rank"] is not None)
    total = sum(1 for i in inferences if i["retrieval_judged"])
    return {"hits": hits, "total": total, "skipped": len(inferences) - total,
            "hit_rate": (hits / total) if total else 0.0,
            "mrr": (rr_sum / total) if total else 0.0}


def _failed_inference() -> dict:
    """用例连检索/生成都没跑成时的占位事实：不进任何分母，也不是"未命中"
    ——它是"没评"。混进 hit 率分母等于把基础设施故障记成检索变差。"""
    return {"rank": None, "retrieval_judged": False, "top_doc": None}


def build_cases_summary(cases: list[dict], inferences: list[dict | None],
                        answers: dict[int, str] | None = None,
                        judges: dict[int, dict] | None = None,
                        case_errors: dict[int, str] | None = None) -> list[dict]:
    """逐用例明细（落 EvalRun.detail 的 JSON）。

    answers/judges 由答案级评测传入（下标对齐 cases）；纯检索回归不传，
    明细里就不出现答案与裁判字段——**与改造前的明细逐字节一致**。
    """
    answers = answers or {}
    judges = judges or {}
    case_errors = case_errors or {}
    details: list[dict] = []
    for index, case in enumerate(cases):
        fact = inferences[index] or _failed_inference()
        entry = {"question": case["question"], "rank": fact["rank"],
                 "hit": (fact["rank"] is not None) if fact["retrieval_judged"] else None,
                 "top_doc": fact["top_doc"],
                 "expected_answer": case.get("expected_answer"),
                 "retrieval_judged": fact["retrieval_judged"]}
        if index in answers:
            entry["answer"] = answers[index]
        if index in judges:
            entry.update({"answer_score": judges[index]["score"],
                          "answer_reason": judges[index]["reason"],
                          "judge_provider": judges[index].get("judge_provider"),
                          "judge_error": judges[index].get("error")})
        if index in case_errors:
            entry["case_error"] = case_errors[index]
        details.append(entry)
    return details


def build_run_record(cases: list[dict], inferences: list[dict | None], *,
                     set_id: str, top_k: int, mode: str = MODE_RETRIEVAL,
                     answers: dict[int, str] | None = None,
                     judges: dict[int, dict] | None = None,
                     case_errors: dict[int, str] | None = None,
                     judge_provider: str | None = None,
                     run_id: str | None = None,
                     created_at: str | None = None) -> dict:
    """汇整一次回归的全部口径（纯函数，不碰 DB）：检索指标 + 答案分 + 明细。

    answer_score = 逐用例裁判分（0~1）的**平均值**，只把判分成功的用例算进
    分母（`judged_count`）；`judge_failed_count` 单独报出来——裁判失败的用例
    不该既拉低平均分又被当成"答得差"，那是两回事。

    inferences 允许含 None（该用例检索/生成就失败了）：这类用例不进任何分母，
    只在明细里留 `case_error`——一条坏用例不该让整批的指标无从产出。
    """
    case_errors = dict(case_errors or {})
    for index, value in enumerate(inferences):
        if value is None:
            case_errors.setdefault(index, "检索或生成失败（该用例未产出结果）")
    safe_inferences = [i or _failed_inference() for i in inferences]
    metrics = aggregate_retrieval(safe_inferences)
    judged = {i: j for i, j in (judges or {}).items() if j.get("score") is not None}
    scores = [j["score"] for j in judged.values()]
    # 没判出来的用例再分两类报：裁判调用/输出坏了 vs 用例压根没给参考答案
    # （T13 回灌形态）——把后者算进"判分失败"会让人以为裁判不稳定。
    judge_failed = [j for j in (judges or {}).values()
                    if j.get("score") is None and j.get("error") == "judge_failed"]
    no_reference = [j for j in (judges or {}).values()
                    if j.get("score") is None and j.get("error") == "no_reference"]
    record = {
        "id": run_id or str(uuid.uuid4()),
        "set_id": set_id,
        "top_k": top_k,
        "mode": mode,
        "hit_rate_raw": metrics["hit_rate"],
        "mrr_raw": metrics["mrr"],
        "hit_rate": round(metrics["hit_rate"], 4),
        "mrr": round(metrics["mrr"], 4),
        "total": metrics["total"],
        "hits": metrics["hits"],
        "skipped": metrics["skipped"],
        "case_count": len(cases),
        "answer_score_raw": (sum(scores) / len(scores)) if scores else None,
        "answer_score": (round(sum(scores) / len(scores), 4) if scores else None),
        "judged_count": len(scores),
        "judge_failed_count": len(judge_failed),
        "no_reference_count": len(no_reference),
        "unjudged_count": len(judges or {}) - len(scores),
        "case_error_count": len(case_errors),
        "judge_provider": judge_provider,
        "details": build_cases_summary(cases, safe_inferences, answers, judges,
                                       case_errors),
    }
    if created_at is not None:
        record["created_at"] = created_at
    return record


def _persist_run(sf, record: dict) -> None:
    run = EvalRun(id=record["id"], set_id=record["set_id"], top_k=record["top_k"],
                  hit_rate=record["hit_rate_raw"], mrr=record["mrr_raw"],
                  total=record["total"],
                  detail=json.dumps(record["details"], ensure_ascii=False),
                  mode=record["mode"], answer_score=record["answer_score_raw"],
                  judge_provider=record["judge_provider"])
    with sf() as s:
        s.add(run)
        s.commit()


def run_eval(sf, retriever, set_id: str, *, top_k: int = 5,
             strategy=None) -> dict | None:
    """对评测集跑一遍检索回归，落 eval_runs 快照并返回结果。
    strategy 传 None=用调用方解析好的 KB 策略跑（与线上问答同路径）。

    逐用例都检索（答案级判分 T15 要用同一批命中块），但 **hit@k/MRR 只统计
    有检索期望的用例**；`total` = 参与检索判分的用例数，未被检索判分的用例如
    数另给 `skipped`——两者都报出来，口径变化在结果里可见。
    """
    with sf() as s:
        row = s.get(EvalSet, set_id)
        if row is None:
            return None
        kb_id, cases = row.kb_id, json.loads(row.cases)

    inferences = infer_retrieval(retriever, kb_id, cases, top_k=top_k,
                                 strategy=strategy)
    record = build_run_record(cases, inferences, set_id=set_id, top_k=top_k,
                              mode=MODE_RETRIEVAL)
    _persist_run(sf, record)
    return _run_response(record, created_at=_created_at(sf, record["id"]))


def _created_at(sf, run_id: str) -> str:
    with sf() as s:
        return s.get(EvalRun, run_id).created_at.isoformat()


def _run_response(record: dict, *, created_at: str | None) -> dict:
    """API/明细响应体：在裸 record 上去掉内部 `*_raw` 字段（不入对外契约）。"""
    out = {k: v for k, v in record.items() if not k.endswith("_raw")}
    out["created_at"] = created_at or record.get("created_at")
    return out


# ---------------- 答案级判分（T15）----------------

# 固定 rubric：0~1 分制 + 一句话理由。刻意不用 1~5/1~10 档——档位越多，
# 同一个裁判模型在相邻档之间的抖动越大，回归曲线上的"提升"就越可能只是噪声；
# 0~1 连续分配合"这条答案覆盖到参考答案多少要点"的直觉，且小模型也稳。
ANSWER_JUDGE_SYSTEM_PROMPT = (
    "你是知识库问答的评测裁判。给你一个问题、一份参考答案和一段待评的模型答案，"
    "请判断模型答案与参考答案的**语义一致程度**：覆盖了多少参考答案的要点、"
    "有没有与参考答案冲突或凭空编造的内容。\n"
    "评分标准（0~1 分，越高越一致）：\n"
    "1.0 = 要点齐全且无冲突；\n"
    "0.7~0.9 = 覆盖主要要点，次要细节略有出入；\n"
    "0.4~0.6 = 只覆盖部分要点，或有明显遗漏；\n"
    "0.1~0.3 = 大面积不符或严重遗漏；\n"
    "0.0 = 与参考答案矛盾、答非所问，或模型直接拒答（未给出任何有效信息）。\n"
    "只输出一个 JSON 对象，不要任何解释性文字或代码围栏，格式为："
    '{"score": 0.8, "reason": "一句话说明扣分理由"}'
)

ANSWER_JUDGE_TEMPLATE = """问题：{question}

参考答案：
{reference}

模型答案：
{answer}

请输出该模型答案的 0~1 分与一句话理由（JSON）。"""

_SCORE_RE = re.compile(r"(?<![\d.])([01](?:\.\d+)?|\.\d+)(?![\d])")


def grade_answer(raw: str) -> tuple[float, str]:
    """解析裁判输出 → (score ∈ [0,1], 一句话理由)。

    主路径是 JSON（见 ANSWER_JUDGE_SYSTEM_PROMPT）。但裁判是小模型时偶发不守
    格式（回一段"0.8 分，因为……"的散文），所以退一步：从原文里抓第一个
    0~1 之间的数当分，并把整段文本首句当理由。**抓不到数就抛 ValueError**——
    这时候"给 0 分"是错的（它可能答得很好，只是裁判没按格式说），必须让该
    用例被标成判分失败、不进答案分分母，而不是静默记 0 把整体分拉低。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("裁判返回空内容")

    body = text
    fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```$", body, re.DOTALL)
    if fence:
        body = fence.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(body[start:end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "score" in data:
            score = _coerce_score(data["score"])
            reason = str(data.get("reason") or "").strip()
            return score, reason or text[:120]

    match = _SCORE_RE.search(body)
    if match is None:
        raise ValueError(f"裁判输出中未找到 0~1 分数: {text[:200]!r}")
    score = _coerce_score(match.group(1))
    return score, _fallback_reason(body, match)


def _fallback_reason(body: str, match: re.Match) -> str:
    """散文兜底时抠"一句话理由"：去掉分数本身与紧随其后的"分"字，
    再取第一句。否则理由会退化成 "0.7 分"（只有分数，没有理由——
    报告里那列就没意义了）。"""
    rest = (body[:match.start()] + body[match.end():]).strip()
    rest = re.sub(r"^[分：:\s，,。.]*", "", rest)
    sentence = re.split(r"[。\n；;]", rest)[0].strip()
    return sentence or body[:120]


def _coerce_score(value) -> float:
    """裁判给的分归一化到 [0,1]：容忍 "0.8"（字符串）、1、0、.7。
    越界（模型偶尔给 8 或 -1）视为格式错——抛出去让该用例标失败，
    不夹取成 1.0/0.0（夹取会把格式错伪装成一个极端的真实评分）。"""
    try:
        score = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"裁判分数不是数字: {value!r}") from e
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"裁判分数越界（应在 0~1）: {score}")
    return score


def list_runs(sf, set_id: str, limit: int = 20) -> list[dict]:
    """历史回归清单（新→旧），前端并排画 hit/mrr 折线做趋势对比。

    答案级回归（mode="answer"）多带 answer_score/judge_provider；老库存量行
    mode 补列后为 NULL，这里统一按 retrieval 报（与升级前行为一致）。
    """
    with sf() as s:
        rows = (s.query(EvalRun).filter(EvalRun.set_id == set_id)
                .order_by(EvalRun.created_at.desc()).limit(limit).all())
        return [{"id": r.id, "top_k": r.top_k,
                 "mode": r.mode or MODE_RETRIEVAL,
                 "hit_rate": round(r.hit_rate, 4), "mrr": round(r.mrr, 4),
                 "answer_score": (round(r.answer_score, 4)
                                  if r.answer_score is not None else None),
                 "judge_provider": r.judge_provider,
                 "total": r.total, "created_at": r.created_at.isoformat()}
                for r in rows]


def get_run(sf, run_id: str) -> dict | None:
    """单次回归的逐用例明细（排查哪些问题掉出了 top-k）。"""
    with sf() as s:
        r = s.get(EvalRun, run_id)
        if r is None:
            return None
        return {"id": r.id, "set_id": r.set_id, "top_k": r.top_k,
                "mode": r.mode or MODE_RETRIEVAL,
                "hit_rate": round(r.hit_rate, 4), "mrr": round(r.mrr, 4),
                "answer_score": (round(r.answer_score, 4)
                                 if r.answer_score is not None else None),
                "judge_provider": r.judge_provider,
                "total": r.total, "details": json.loads(r.detail),
                "created_at": r.created_at.isoformat()}


def get_run_record(sf, run_id: str) -> dict | None:
    """报告渲染用的完整口径：get_run 的字段 + 由明细重算的用例数/判分数。

    用例数（case_count）与判分数（judged_count）从已落库的明细里数出来，
    不额外存列——明细是唯一真相源，避免"列里的数"和"明细里的数"对不上。
    """
    run = get_run(sf, run_id)
    if run is None:
        return None
    details = run["details"]
    judged = [d for d in details if d.get("answer_score") is not None]
    run["case_count"] = len(details)
    run["judged_count"] = len(judged)
    run["judge_failed_count"] = len(
        [d for d in details if d.get("judge_error") == "judge_failed"])
    run["no_reference_count"] = len(
        [d for d in details if d.get("judge_error") == "no_reference"])
    run["case_error_count"] = len([d for d in details if d.get("case_error")])
    return run


# ---------------- 导出报告（T15 spec §4）----------------

REPORT_TITLE = "评测回归报告"


def render_report(record: dict) -> str:
    """把一次回归渲染成 Markdown 报告（.md / .docx 同一份源，见
    export_docx.markdown_to_docx——docx 由它按需转换，不另写一套）。

    **刻意不只在标题里写一个总分**：单看一个 0.82 既不知道它由多少条用例得出，
    也不知道检索侧同时是好是坏、裁判是哪个模型、下次该拿什么口径复测。报告里
    这几项都单列（spec §4 的硬要求），复用/对比才有意义。
    """
    mode = record.get("mode") or MODE_RETRIEVAL
    answer_mode = mode == MODE_ANSWER
    lines = [
        f"# {REPORT_TITLE}",
        "",
        "## 概要",
        "",
        f"- 回归 id：`{record['id']}`",
        f"- 评测集：`{record['set_id']}`",
        f"- 判分口径：{'答案级（检索 + 生成 + 裁判打分）' if answer_mode else '仅检索（不评生成）'}",
        f"- 运行时间：{record.get('created_at') or '(未记录)'}",
        f"- top_k：{record['top_k']}",
        f"- **用例总数：{record.get('case_count', len(record.get('details', [])))}**"
        f"（参与检索判分 {record['total']}，未参与检索判分"
        f" {record.get('skipped', 0)}，检索命中 {record.get('hits', 0)}）",
        "",
        "## 指标",
        "",
        "| 指标 | 数值 | 口径 |",
        "| --- | --- | --- |",
        f"| hit@{record['top_k']} | {_pct(record['hit_rate'])} | 命中用例 / 参与检索判分的用例 |",
        f"| MRR | {_num(record['mrr'])} | 平均(1/首个命中块名次)，未命中记 0 |",
    ]
    if answer_mode:
        score = record.get("answer_score")
        lines.append(
            f"| 答案分（0~1） | {_num(score) if score is not None else '未产出'} | "
            f"逐用例裁判分均值，分母为判分成功的 {record.get('judged_count', 0)} 条 |")
        lines.append(
            f"| 裁判模型 | `{record.get('judge_provider') or '(用活跃 provider)'}` | "
            f"judge_provider，换裁判模型会改变答案分口径 |")
        if record.get("judge_failed_count"):
            lines.append(
                f"| 判分失败 | {record['judge_failed_count']} 条 | "
                f"裁判调用/输出异常，不进答案分分母（明细里标 judge_error） |")
        if record.get("no_reference_count"):
            lines.append(
                f"| 无参考答案 | {record['no_reference_count']} 条 | "
                f"用例没给 expected_answer，无从判分（与判分失败不是一回事） |")
        if record.get("case_error_count"):
            lines.append(
                f"| 用例执行失败 | {record['case_error_count']} 条 | "
                f"检索/生成没跑成，不进任何分母（明细里标 case_error） |")
    else:
        lines.append(
            "| 答案分（0~1） | 未评测 | 答案级判分默认关闭"
            "（evals.answer_judge.enabled=false），本次只评检索 |")

    lines += ["", "## 复测口径", "",
              "照下面这条命令/操作复跑，指标才与本次可比：", ""]
    if answer_mode:
        lines.append(
            f"- 开 `evals.answer_judge.enabled=true`，裁判 provider 仍为 "
            f"`{record.get('judge_provider') or '(活跃 provider)'}`")
        lines.append(f"- `POST /api/eval-sets/{record['set_id']}/run`，"
                     f"body `{{\"mode\": \"answer\", \"top_k\": {record['top_k']}}}`")
        lines.append("- 换裁判模型 / 换参考答案 / 换答案长度截断都是**口径变更**，"
                     "与本次不可直接比较（报告里记的是 judge_provider）")
    else:
        lines.append(f"- `POST /api/eval-sets/{record['set_id']}/run`，"
                     f"body `{{\"top_k\": {record['top_k']}}}`"
                     "（mode 缺省即 retrieval）")
        lines.append("- 该集的检索期望与文档集（同一份语料）需与本次相同；"
                     "换库/换向量模型后对比无意义")
    lines.append("- 拒答阈值、重排/多路召回策略由该库当时的配置决定，"
                 "改过检索配置就要重新记基线")

    details = record.get("details") or []
    lines += ["", "## 逐用例明细", ""]
    if answer_mode:
        lines += ["| # | 用例 | 检索 | 答案分 | 裁判理由 |",
                  "| --- | --- | --- | --- | --- |"]
        for i, d in enumerate(details, start=1):
            lines.append(
                f"| {i} | {_cell(d['question'])} | {_retrieval_cell(d)} | "
                f"{'—' if d.get('answer_score') is None else _num(d['answer_score'])} | "
                f"{_cell(d.get('answer_reason') or '')} |")
    else:
        lines += ["| # | 用例 | 检索结果 | 期望 |", "| --- | --- | --- | --- |"]
        for i, d in enumerate(details, start=1):
            lines.append(
                f"| {i} | {_cell(d['question'])} | {_retrieval_cell(d)} | "
                f"{_cell(_expectation_cell(d))} |")

    missed = [d for d in details if d.get("hit") is False]
    if missed:
        lines += ["", f"## 未命中用例（{len(missed)} 条）", ""]
        for d in missed:
            lines.append(f"- {d['question']}（top1：{d['top_doc'] or '无结果'}）")
    return "\n".join(lines) + "\n"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _num(value) -> str:
    """数值统一 4 位小数：与落库/API 的 round(...,4) 同一精度，
    避免报告里的数与历史清单里的数看起来不一致。"""
    return "—" if value is None else f"{float(value):.4f}"


def _cell(text) -> str:
    """Markdown 表格单元格转义：竖线会截断列，换行会拆行。"""
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _retrieval_cell(detail: dict) -> str:
    if detail.get("case_error"):
        return f"用例执行失败（{detail['case_error']}）"
    if not detail.get("retrieval_judged"):
        return "未参与检索判分（用例只带参考答案）"
    if detail.get("rank") is None:
        return f"未命中（top1：{detail.get('top_doc') or '无结果'}）"
    return f"命中 #{detail['rank']}（{detail.get('top_doc') or '—'}）"


def _expectation_cell(detail: dict) -> str:
    return detail.get("expected_answer") or "（无参考答案）"
