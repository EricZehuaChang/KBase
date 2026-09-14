"""标问库领域逻辑（T13）：人工策展的"标准问题 + 标准答案"，先审核、后受控使用。

为什么独立成模块：这张表带一条**红线**（见 models.StandardAnswer 的 docstring）。
审核通过的标问**只有两个去处**：
(a) 回灌评测集（`append_to_eval_set`）——用 run_eval 检验"这条标准问题在库里
    真的检索得到依据"，检索不行就说明该补文档而不是该加标问；
(b) 可选地作为**问答型文档**走正常摄取管道：把标问写成 markdown 走
    `POST /api/kb/{kb_id}/documents` 即可。这条**不需要新代码**，走的是与普通
    文档完全相同的分块/向量化/ACL/引用路径，所以仍可溯源、仍受库级权限约束。

**严禁"相似度命中就绕过检索直接返回答案"的代码路径**（方案已定案，不做）。
本模块因此刻意**不提供任何检索/召回函数**，只有 CRUD 与上面两个出口；
全仓 grep 守门人在 tests/test_standard_answers.py 里钉着这条红线。

状态机照抄 documents 的人工审核（routes/kb.py 的 review_document +
ingest/pipeline.py 的 approve_document）：创建一律 `pending_review` →
`approved` | `rejected`；**非 pending_review 再审核抛 ValueError，路由转 409**。
回灌评测集额外要求已 `approved`（未过审的标问不得进入评测口径）。
"""
import json
import uuid
from datetime import datetime

from kbase.models import EvalSet, QaOutcome, StandardAnswer


def _out(row: StandardAnswer) -> dict:
    """行 → 对外字典。similar_questions 在库里是 JSON 文本，出口给数组。"""
    return {
        "id": row.id,
        "kb_id": row.kb_id,
        "question": row.question,
        "similar_questions": json.loads(row.similar_questions or "[]"),
        "answer": row.answer,
        "category": row.category,
        "status": row.status,
        "source": row.source,
        "source_outcome_id": row.source_outcome_id,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
    }


def create(sf, kb_id: str, *, question: str, answer: str = "",
           similar_questions: list[str] | None = None,
           category: str | None = None, source: str = "manual",
           source_outcome_id: str | None = None,
           created_by: str | None = None) -> dict:
    """录入标问。**恒为 pending_review**——创建者不能自审自过，审核走 review()。

    answer 允许为空串：一键提取（路由里的 POST /api/stats/outcomes/{id}/
    standard-answer，问题原文由归因行预填）时运营只捞到问题，标准答案由审核人
    在 review(decision="approve", answer=...) 时补齐。
    """
    row = StandardAnswer(
        id=str(uuid.uuid4()), kb_id=kb_id, question=question,
        similar_questions=json.dumps(similar_questions or [], ensure_ascii=False),
        answer=answer, category=category, status="pending_review",
        source=source, source_outcome_id=source_outcome_id,
        created_by=created_by)
    with sf() as s:
        s.add(row)
        s.commit()
        return _out(row)


def list_rows(sf, kb_id: str, status: str | None = None) -> list[dict]:
    """某库的标问清单（新→旧）。status=None 全量，否则按状态过滤
    （pending_review / approved / rejected）。"""
    with sf() as s:
        q = s.query(StandardAnswer).filter(StandardAnswer.kb_id == kb_id)
        if status:
            q = q.filter(StandardAnswer.status == status)
        rows = q.order_by(StandardAnswer.created_at.desc()).all()
        return [_out(r) for r in rows]


def get_row(sf, sa_id: str) -> dict | None:
    """按 id 取标问（路由据此拿 kb_id 过库级守卫，再过状态机）。"""
    with sf() as s:
        row = s.get(StandardAnswer, sa_id)
        return _out(row) if row is not None else None


def review(sf, sa_id: str, *, decision: str, answer: str | None = None,
           reviewed_by: str | None = None) -> dict | None:
    """人工审核：pending_review → approved | rejected。返回 None=标问不存在。

    状态不是 pending_review 时抛 ValueError（路由转 409，与 documents 的
    review_document / approve_document 完全一致：不允许反复审核、不允许
    改判终态）。answer 给了就以人工核对稿覆盖（审核人可顺手修正答案）。
    """
    with sf() as s:
        row = s.get(StandardAnswer, sa_id)
        if row is None:
            return None
        if row.status != "pending_review":
            raise ValueError(f"仅待审核状态可审核，当前: {row.status}")
        if answer is not None:
            row.answer = answer
        row.status = "approved" if decision == "approve" else "rejected"
        row.reviewed_by = reviewed_by
        row.reviewed_at = datetime.utcnow()
        s.commit()
        return _out(row)


def outcome_source(sf, outcome_id: str) -> tuple[str | None, str] | None:
    """读一条 T12 归因行，返回 (kb_id, question)；行不存在返回 None。

    T13 直接读 QaOutcome 模型，**不依赖 T12 的路由/模块是否存在**——归因行
    本身是共享基座（models.py，M3 已落库），运营看板的一键提取因此可以独立
    上线。kb_id 可能为 NULL（多库联查的归因行只记 kb_ids），此时调用方需显式
    给出目标库，否则无从做库级守卫、也无从归属标问。
    """
    with sf() as s:
        row = s.get(QaOutcome, outcome_id)
        if row is None:
            return None
        return (row.kb_id, row.question)


def append_to_eval_set(sf, sa_id: str, set_id: str, *, expect_doc: str | None = None,
                       expect_text: str | None = None,
                       expected_answer: str | None = None) -> dict | None:
    """把标问作为一条评测用例追加进评测集（红线出口 a）。返回 None=标问或
    评测集不存在。

    只有 approved 的标问能回灌——未过审（pending_review/rejected）抛
    ValueError（路由转 409），否则"审核"这道闸门就有了绕过口。
    expected_answer 缺省用标问自己的标准答案兜底：这条用例的检索期望
    （expect_doc/expect_text）由调用方给，答案级判分（T15）则用参考答案。
    """
    with sf() as s:
        row = s.get(StandardAnswer, sa_id)
        if row is None:
            return None
        if row.status != "approved":
            raise ValueError(f"仅审核通过的标问可回灌评测集，当前: {row.status}")
        eval_set = s.get(EvalSet, set_id)
        if eval_set is None:
            return None
        cases = json.loads(eval_set.cases)
        case = {"question": row.question}
        if expect_doc:
            case["expect_doc"] = expect_doc
        if expect_text:
            case["expect_text"] = expect_text
        case["expected_answer"] = (expected_answer if expected_answer is not None
                                   else row.answer)
        cases.append(case)
        eval_set.cases = json.dumps(cases, ensure_ascii=False)
        s.commit()
        return {"set_id": set_id, "kb_id": eval_set.kb_id,
                "case_count": len(cases), "case": case}
