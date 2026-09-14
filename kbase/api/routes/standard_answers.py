"""标问库路由（T13）：人工策展的标准问答——录入 / 审核 / 两个受控出口。

红线（见 models.StandardAnswer 与 kbase/standard_answers.py 的模块 docstring）：
审核通过的标问**只用于两处**——回灌评测集，或可选地作为问答型文档走正常摄取
管道。**本文件刻意不提供任何"问答时先查标问库、相似度命中就直接返回答案"的
入口**：这里只有 CRUD 与 to-eval-set，检索/问答路径（routes/query.py、
kbase/rag/*、kbase_mcp/* 的 answer 工具）对标问库一无所知；全仓 grep 守门人在
tests/test_standard_answers.py::test_no_direct_return_path_in_answer_pipeline 钉着。

权限：录入与审核 editor 起步（审核决定标问能否被使用，与文档审核同一门槛）；
只读列表 viewer 即可。库级可见性一律走 T02 的 KbGuard（ACL + API Key scope，
库外资源统一 404，不泄漏存在性）。
"""
from typing import Literal

from fastapi import HTTPException, Query, Request

from kbase import evals
from kbase import standard_answers as sa
from kbase.api.guards import KbGuard
from kbase.api.routes import RouteDeps
from kbase.api.schemas import (OutcomeToStandardAnswer, StandardAnswerIn,
                               StandardAnswerReview, StandardAnswerToEvalSet)
from kbase.api.services import Services
from kbase.errors import AppError


def _actor_name(request: Request) -> str | None:
    """审计用 actor 名（created_by / reviewed_by）。actor 结构见 auth/deps.py：
    用户名在 "name"（与 openai_compat 的 write_audit 同一取法）。"""
    actor = getattr(request.state, "actor", None) or {}
    return actor.get("name") or actor.get("user_id")


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf = svc.sf
    guard = KbGuard(sf)

    @router.post("/kb/{kb_id}/standard-answers",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def create_standard_answer(kb_id: str, body: StandardAnswerIn, request: Request):
        """录入标问：**恒为 pending_review**（请求体没有 status——创建者不能
        自录自过，与文档审核同一条规矩）。"""
        guard.kb(kb_id, request)
        return sa.create(sf, kb_id, question=body.question, answer=body.answer,
                         similar_questions=body.similar_questions,
                         category=body.category, source=body.source,
                         created_by=_actor_name(request))

    @router.get("/kb/{kb_id}/standard-answers", dependencies=[deps.require_viewer])
    def list_standard_answers(kb_id: str, request: Request,
                              status: Literal["pending_review", "approved",
                                              "rejected"] | None = Query(default=None)):
        """某库标问清单，可按状态过滤（pending_review / approved / rejected，
        取值写死——状态是状态机定的，不接自由文本）。空/缺省=全量。"""
        guard.kb(kb_id, request)
        return sa.list_rows(sf, kb_id, status=status)

    @router.put("/standard-answers/{sa_id}/review",
                dependencies=[deps.require_editor, deps.audit_mutation])
    def review_standard_answer(sa_id: str, body: StandardAnswerReview,
                               request: Request):
        """人工审核：照抄 routes/kb.py 的 review_document——**仅 pending_review
        可审核，其余状态 409**（不允许反复审核、不允许改判终态）。decision=
        approve 时 body.answer 给了就以人工核对稿覆盖标准答案。"""
        row = sa.get_row(sf, sa_id)
        if row is None:
            raise AppError("error.standard_answer_not_found", "标问不存在: {id}",
                           status=404, id=sa_id)
        # 先过库级守卫再碰状态机：无权者不该从 409/404 的差别里看出标问状态
        guard.kb(row["kb_id"], request)
        try:
            return sa.review(sf, sa_id, decision=body.decision, answer=body.answer,
                             reviewed_by=_actor_name(request))
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @router.post("/stats/outcomes/{outcome_id}/standard-answer",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def standard_answer_from_outcome(outcome_id: str,
                                     body: OutcomeToStandardAnswer, request: Request):
        """运营看板一键提取（T12 归因行 → 标问）：**问题原文由归因行预填**，
        运营只补答案。落库仍是 pending_review——提取不等于过审。

        只读 QaOutcome 模型本身，不依赖 T12 的路由模块（归因表是 M3 已落库的
        共享基座），T12 的接口晚到也不影响这个入口。多库联查的归因行 kb_id 为
        NULL，此时必须显式指定 kb_id，否则无从做库级守卫也无从归属标问。
        """
        found = sa.outcome_source(sf, outcome_id)
        if found is None:
            raise AppError("error.outcome_not_found", "归因记录不存在: {id}",
                           status=404, id=outcome_id)
        source_kb, question = found
        # 归因行自己记着库就以它为准（多库联查行才用请求体给的库）
        kb_id = source_kb or body.kb_id
        if not kb_id:
            raise AppError("error.kb_required",
                           "该归因记录未关联知识库（多库联查），请指定 kb_id",
                           status=422)
        guard.kb(kb_id, request)
        return sa.create(sf, kb_id, question=question, answer=body.answer,
                         similar_questions=body.similar_questions,
                         category=body.category, source="ops",
                         source_outcome_id=outcome_id,
                         created_by=_actor_name(request))

    @router.post("/standard-answers/{sa_id}/to-eval-set",
                 dependencies=[deps.require_editor, deps.audit_mutation])
    def standard_answer_to_eval_set(sa_id: str, body: StandardAnswerToEvalSet,
                                    request: Request):
        """红线出口 (a)：把标问追加为一条评测用例——用 run_eval 验证"这条标准
        问题在库里确实检索得到依据"。**仅 approved 可回灌**（未过审 409，否则
        审核这道闸门就有绕过口）；标问与评测集必须同库（跨库用例检索的是另一个
        库的语料，判分没有意义）。

        另一个出口是"作为问答型文档走正常摄取管道"，那不需要新端点：把标问写成
        markdown 走 POST /api/kb/{kb_id}/documents 即可。
        """
        row = sa.get_row(sf, sa_id)
        if row is None:
            raise AppError("error.standard_answer_not_found", "标问不存在: {id}",
                           status=404, id=sa_id)
        guard.kb(row["kb_id"], request)
        eval_set = evals.get_set(sf, body.set_id)
        if eval_set is None:
            raise AppError("error.eval_set_not_found", "评测集不存在: {id}",
                           status=404, id=body.set_id)
        guard.kb(eval_set.kb_id, request)
        if eval_set.kb_id != row["kb_id"]:
            raise AppError("error.cross_kb_eval_case",
                           "标问与评测集不属于同一知识库", status=422)
        try:
            return sa.append_to_eval_set(
                sf, sa_id, body.set_id, expect_doc=body.expect_doc,
                expect_text=body.expect_text, expected_answer=body.expected_answer)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
