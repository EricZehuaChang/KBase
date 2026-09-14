"""标问库（T13）：状态机 409、审核前后检索/问答口径不变、回灌评测集真的被
run_eval 覆盖、库外 404 与角色门槛，以及**红线守门人**——全仓 grep 断言不存在
"相似度命中就绕过检索直接返回标问答案"的代码路径。

红线（models.StandardAnswer 的 docstring 已定案）：审核通过的标问只用于
(a) 回灌评测集、(b) 可选地作为问答型文档走正常摄取管道。本文件末尾两个
守门人测试是**闸门**而不是说明：谁把标问库接进检索/问答/生成/对外入口，
或者新增一个标问库引用方而不登记，CI 立刻红。
"""
import re
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from kbase.models import QaOutcome
from tests.test_api import CFG, FakeLLM

DOC_A = "# 补贴办法\n\n连续工作满两年可申领住房补贴。\n"
DOC_B = "# 考勤制度\n\n迟到三次记旷工半天。\n"

# 标问专用答案：这句话**不在任何文档里**，一旦它出现在检索命中或问答回答里，
# 就说明有人把标问库接进了回答路径（红线）。
SA_QUESTION = "住房补贴怎么申领"
SA_ANSWER = "标问专用答案：入职满两年后每月 800 元。"


def _client(tmp_path, fake_embedder, *, auth="off", monkeypatch=None):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    if auth == "on" and monkeypatch is not None:
        monkeypatch.setenv("KBASE_ADMIN_PASSWORD", "admin-pw")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth=auth)
    return app, TestClient(app)


@pytest.fixture
def kb_ready(tmp_path, fake_embedder):
    """一个库两份文档 + 一个 TestClient（auth=off，合成超管）。"""
    _app, c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "政策库"}).json()["id"]
    c.post(f"/api/kb/{kb}/documents", files=[
        ("files", ("补贴.md", DOC_A.encode("utf-8"), "text/markdown")),
        ("files", ("考勤.md", DOC_B.encode("utf-8"), "text/markdown")),
    ])
    return c, kb


def _create(c, kb, **over):
    body = {"question": SA_QUESTION, "answer": SA_ANSWER,
            "similar_questions": ["补贴啥时候能领"], "category": "福利"}
    body.update(over)
    r = c.post(f"/api/kb/{kb}/standard-answers", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _approve(c, sa_id, **body):
    r = c.put(f"/api/standard-answers/{sa_id}/review",
              json={"decision": "approve", **body})
    assert r.status_code == 200, r.text
    return r.json()


def _sse(c, kb, question):
    """问答走 SSE：整段响应体（citations→token*→done）就是"问答结果"，
    前后逐字节比较即"完全不变"。"""
    r = c.post(f"/api/kb/{kb}/query", json={"question": question})
    assert r.status_code == 200, r.text
    return r.text


def _search(c, kb, query):
    r = c.post(f"/api/kb/{kb}/search", json={"query": query, "debug": True})
    assert r.status_code == 200, r.text
    return r.json()


def _add_outcome(app, *, kb_id, question, bucket="empty_retrieval"):
    """直接落一行 T12 归因记录（T12 的接口不参与本测试：T13 的一键提取只读
    QaOutcome 模型本身，这样两边可以各自独立上线）。"""
    oid = str(uuid.uuid4())
    with app.state.svc.sf() as s:
        s.add(QaOutcome(id=oid, ts=datetime.utcnow(), channel="web", kb_id=kb_id,
                        bucket=bucket, retrieved_count=0, usable_count=0,
                        question=question))
        s.commit()
    return oid


# ---- 录入 / 列表 / 状态机 ----

def test_create_always_pending_review_and_list_filterable(kb_ready):
    c, kb = kb_ready
    row = _create(c, kb)
    assert row["status"] == "pending_review"      # 创建者不能自录自过
    assert row["source"] == "manual"
    assert row["similar_questions"] == ["补贴啥时候能领"]
    assert row["reviewed_at"] is None

    assert len(c.get(f"/api/kb/{kb}/standard-answers").json()) == 1
    assert len(c.get(f"/api/kb/{kb}/standard-answers?status=pending_review").json()) == 1
    assert c.get(f"/api/kb/{kb}/standard-answers?status=approved").json() == []
    # 状态取值写死：拼错的状态要报错，而不是静默返回空清单（后端状态机没有
    # 这个状态，返回空会让人以为"标问丢了"）
    assert c.get(f"/api/kb/{kb}/standard-answers?status=pendin").status_code == 422

    _approve(c, row["id"])
    assert c.get(f"/api/kb/{kb}/standard-answers?status=pending_review").json() == []
    approved = c.get(f"/api/kb/{kb}/standard-answers?status=approved").json()
    assert [a["id"] for a in approved] == [row["id"]]


def test_review_state_machine_409(kb_ready):
    """照抄文档审核：只有 pending_review 能审，其余状态一律 409——
    不允许反复审核，也不允许把已批准的标问改判成 rejected。"""
    c, kb = kb_ready
    row = _create(c, kb)
    assert _approve(c, row["id"])["status"] == "approved"

    again = c.put(f"/api/standard-answers/{row['id']}/review",
                  json={"decision": "reject"})
    assert again.status_code == 409
    # 409 文案说清"当前是什么状态"，否则前端只能显示一句无信息量的拒绝
    assert "仅待审核状态可审核" in again.json()["detail"]
    assert "approved" in again.json()["detail"]

    rejected = _create(c, kb, question="年假几天")
    r = c.put(f"/api/standard-answers/{rejected['id']}/review",
              json={"decision": "reject"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert c.put(f"/api/standard-answers/{rejected['id']}/review",
                 json={"decision": "approve"}).status_code == 409
    # 审核不存在的标问：404（不是 409）
    assert c.put("/api/standard-answers/no-such-id/review",
                 json={"decision": "approve"}).status_code == 404


def test_review_can_overwrite_answer(kb_ready):
    """人工核对稿为准：审核时给 answer 即覆盖（一键提取的标问答案可能为空，
    由审核人补齐）。"""
    c, kb = kb_ready
    row = _create(c, kb, answer="")
    out = _approve(c, row["id"], answer="连续工作满两年即可申领。")
    assert out["answer"] == "连续工作满两年即可申领。"
    assert out["reviewed_by"] is not None and out["reviewed_at"] is not None


# ---- 红线：审核前后检索与问答结果完全不变 ----

def test_retrieval_and_qa_unchanged_before_and_after_approval(kb_ready):
    """标问库不是检索源、也不是回答源：pending 与 approved 两种状态下，
    检索 blocks 与问答 SSE 整段响应都与"没有标问"时逐字节相同。"""
    c, kb = kb_ready
    before_search = _search(c, kb, SA_QUESTION)
    before_qa = _sse(c, kb, SA_QUESTION)
    assert SA_ANSWER not in before_qa          # 基线里当然没有标问答案

    row = _create(c, kb, question=SA_QUESTION, answer=SA_ANSWER,
                  similar_questions=["住房补贴申领", "补贴怎么领"])
    assert _search(c, kb, SA_QUESTION) == before_search
    assert _sse(c, kb, SA_QUESTION) == before_qa

    _approve(c, row["id"])
    assert _search(c, kb, SA_QUESTION) == before_search
    assert _sse(c, kb, SA_QUESTION) == before_qa
    # 相似问法同样不能凭空召回标问（库里没有这份答案文本）
    assert SA_ANSWER not in str(_search(c, kb, "补贴怎么领"))
    assert SA_ANSWER not in _sse(c, kb, SA_QUESTION)


# ---- 红线出口 (a)：回灌评测集，run_eval 真的覆盖到 ----

def test_approved_answer_feeds_eval_set_and_run_eval_covers_it(kb_ready):
    c, kb = kb_ready
    row = _create(c, kb, question=SA_QUESTION, answer=SA_ANSWER)

    # 未过审不得回灌（否则"审核"这道闸门就有绕过口）
    set_id = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "标问回归集",
        "cases": [{"question": "迟到怎么处理", "expect_text": "旷工半天"}]}).json()["id"]
    pending = c.post(f"/api/standard-answers/{row['id']}/to-eval-set",
                     json={"set_id": set_id, "expect_text": "住房补贴"})
    assert pending.status_code == 409
    assert "仅审核通过的标问可回灌评测集" in pending.json()["detail"]
    assert "pending_review" in pending.json()["detail"]

    _approve(c, row["id"])
    r = c.post(f"/api/standard-answers/{row['id']}/to-eval-set",
               json={"set_id": set_id, "expect_text": "住房补贴"})
    assert r.status_code == 200, r.text
    assert r.json()["case_count"] == 2
    # 标问原文成为用例问题；expected_answer 缺省用标问自己的标准答案
    assert r.json()["case"] == {"question": SA_QUESTION, "expect_text": "住房补贴",
                                "expected_answer": SA_ANSWER}

    run = c.post(f"/api/eval-sets/{set_id}/run", json={"top_k": 5}).json()
    assert run["case_count"] == 2 and run["total"] == 2 and run["skipped"] == 0
    added = next(d for d in run["details"] if d["question"] == SA_QUESTION)
    assert added["hit"] is True and added["rank"] >= 1
    assert added["expected_answer"] == SA_ANSWER
    assert run["hits"] == 2                    # 标问那条真的被 run_eval 判到了

    # 跨库回灌没有意义（检索的是另一个库的语料）→ 422
    kb2 = c.post("/api/kb", json={"name": "另一个库"}).json()["id"]
    other_set = c.post(f"/api/kb/{kb2}/eval-sets", json={
        "name": "他库集", "cases": [{"question": "q", "expect_text": "x"}]}).json()["id"]
    assert c.post(f"/api/standard-answers/{row['id']}/to-eval-set",
                  json={"set_id": other_set, "expect_text": "住房补贴"}).status_code == 422
    # 评测集不存在 → 404
    assert c.post(f"/api/standard-answers/{row['id']}/to-eval-set",
                  json={"set_id": "no-such-set"}).status_code == 404


def test_answer_only_case_is_not_counted_as_retrieval_miss(kb_ready):
    """只带 expected_answer 的用例（T13 回灌时不给检索期望）进不了检索口径的
    分母：它检索侧无从判中，算成 miss 会凭空拉低 hit 率、把回归曲线带偏。"""
    c, kb = kb_ready
    set_id = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "混合集",
        "cases": [{"question": "迟到怎么处理", "expect_text": "旷工半天"}]}).json()["id"]
    row = _create(c, kb)
    _approve(c, row["id"])
    assert c.post(f"/api/standard-answers/{row['id']}/to-eval-set",
                  json={"set_id": set_id}).status_code == 200

    run = c.post(f"/api/eval-sets/{set_id}/run", json={}).json()
    assert run["case_count"] == 2 and run["total"] == 1 and run["skipped"] == 1
    assert run["hit_rate"] == 1.0
    skipped = next(d for d in run["details"] if d["question"] == SA_QUESTION)
    assert skipped["retrieval_judged"] is False and skipped["hit"] is None
    assert skipped["expected_answer"] == SA_ANSWER   # 参考答案仍带出来给答案级判分用


# ---- T12 归因行一键提取（只读 QaOutcome 模型，不依赖 T12 的路由）----

def test_extract_from_outcome_prefills_question(tmp_path, fake_embedder):
    app, c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    oid = _add_outcome(app, kb_id=kb, question="年假有几天")
    r = c.post(f"/api/stats/outcomes/{oid}/standard-answer",
               json={"answer": "入职满一年 5 天。", "category": "假期"})
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["question"] == "年假有几天"        # 问题由归因行预填
    assert row["source"] == "ops" and row["source_outcome_id"] == oid
    assert row["status"] == "pending_review"      # 提取不等于过审

    # 归因行不存在 → 404
    assert c.post("/api/stats/outcomes/no-such-outcome/standard-answer",
                  json={}).status_code == 404

    # 多库联查的归因行 kb_id 为 NULL：给了 kb_id 才能归属，没给则 422
    loose = _add_outcome(app, kb_id=None, question="多库问题")
    assert c.post(f"/api/stats/outcomes/{loose}/standard-answer",
                  json={}).status_code == 422
    ok = c.post(f"/api/stats/outcomes/{loose}/standard-answer",
                json={"kb_id": kb})
    assert ok.status_code == 200 and ok.json()["kb_id"] == kb


# ---- 权限：库外 404 + 角色门槛（走 T02 的 KbGuard，不手搓 ACL）----

@pytest.fixture
def acl_app(tmp_path, fake_embedder, monkeypatch):
    """auth=on：kb_public（无 grant）+ kb_restricted（只授权给 other）。"""
    app, admin = _client(tmp_path, fake_embedder, auth="on", monkeypatch=monkeypatch)
    assert admin.post("/api/auth/login",
                      json={"username": "admin", "password": "admin-pw"}).status_code == 200
    kb_public = admin.post("/api/kb", json={"name": "公开库"}).json()["id"]
    kb_restricted = admin.post("/api/kb", json={"name": "受限库"}).json()["id"]
    other = admin.post("/api/users", json={"username": "other", "role": "viewer",
                                           "password": "other-pw"}).json()
    admin.put(f"/api/kb/{kb_restricted}/grants", json={"user_ids": [other["id"]]})
    admin.post("/api/users", json={"username": "ed0", "role": "editor",
                                   "password": "ed0-pw"})
    admin.post("/api/users", json={"username": "v0", "role": "viewer",
                                   "password": "v0-pw"})
    return app, admin, kb_public, kb_restricted


def _login(app, username, password):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_out_of_scope_kb_is_404_everywhere(acl_app):
    app, admin, kb_public, kb_restricted = acl_app
    # 受限库里的标问（admin 建，用于测"他库标问"）
    hidden = admin.post(f"/api/kb/{kb_restricted}/standard-answers",
                        json={"question": "禁区问题", "answer": "禁区答案"}).json()
    ed = _login(app, "ed0", "ed0-pw")

    # 无权 editor 对受限库：录入 / 列表 / 审核 / 回灌 一律 404（不泄漏存在性）
    assert ed.post(f"/api/kb/{kb_restricted}/standard-answers",
                   json={"question": "q", "answer": "a"}).status_code == 404
    assert ed.get(f"/api/kb/{kb_restricted}/standard-answers").status_code == 404
    assert ed.put(f"/api/standard-answers/{hidden['id']}/review",
                  json={"decision": "approve"}).status_code == 404
    set_hidden = admin.post(f"/api/kb/{kb_restricted}/eval-sets", json={
        "name": "他库集", "cases": [{"question": "q", "expect_text": "x"}]}).json()["id"]
    assert ed.post(f"/api/standard-answers/{hidden['id']}/to-eval-set",
                   json={"set_id": set_hidden}).status_code == 404
    # 不存在的库同样 404
    assert ed.get("/api/kb/no-such-kb/standard-answers").status_code == 404

    # 公开库（无 grant）对授权 editor 正常可用——防"守卫写成全拒"
    mine = ed.post(f"/api/kb/{kb_public}/standard-answers",
                   json={"question": "公开库问题", "answer": "a"})
    assert mine.status_code == 200
    assert len(ed.get(f"/api/kb/{kb_public}/standard-answers").json()) == 1
    assert ed.put(f"/api/standard-answers/{mine.json()['id']}/review",
                  json={"decision": "approve"}).status_code == 200

    # 归因行落在受限库：无权 editor 一键提取同样 404
    oid = _add_outcome(app, kb_id=kb_restricted, question="禁区缺口")
    assert ed.post(f"/api/stats/outcomes/{oid}/standard-answer",
                   json={}).status_code == 404


def test_role_threshold_viewer_cannot_write(acl_app):
    """录入与审核 editor 起步（审核决定标问能否被使用）；只读列表 viewer 可用。"""
    app, admin, kb_public, _kb_restricted = acl_app
    viewer = _login(app, "v0", "v0-pw")
    assert viewer.post(f"/api/kb/{kb_public}/standard-answers",
                       json={"question": "q", "answer": "a"}).status_code == 403
    row = admin.post(f"/api/kb/{kb_public}/standard-answers",
                     json={"question": "q", "answer": "a"}).json()
    assert viewer.put(f"/api/standard-answers/{row['id']}/review",
                      json={"decision": "approve"}).status_code == 403
    assert viewer.get(f"/api/kb/{kb_public}/standard-answers").status_code == 200


# ---- 红线守门人（全仓 grep，闸门测试）----

ROOT = Path(__file__).resolve().parents[1]
# 标问库的任何标识：类名/表名/路由前缀/字段名（similar_questions 是"相似度
# 命中就返回答案"这条红线唯一会用到的字段，单独盯住）
_PATTERN = re.compile(
    r"StandardAnswer|standard_answer|standard-answers|similar_questions")
_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
              ".codegraph", ".mypy_cache", "web", "web-app", "docs", "data",
              "build", "dist"}

# **管理面**白名单：允许引用标问库的生产代码（录入/审核/回灌/迁移/只读展示）。
# 新增引用方 = 需要人工确认它不是回答路径，所以这里是白名单而不是提醒。
_MANAGEMENT_PLANE = {
    "kbase/models.py",                     # 表声明（红线写在 docstring 里）
    "kbase/migrations.py",                 # 新表由 create_all 建，注释提及
    "kbase/standard_answers.py",           # T13 领域模块（只有 CRUD + 两个出口）
    "kbase/api/schemas.py",                # 请求体
    "kbase/api/main.py",                   # 路由注册
    "kbase/evals.py",                      # expected_answer 可选字段
    "kbase/api/routes/standard_answers.py",
    # T14 的 MCP 工具：**只提交、不生效**（提交进审核队列）。这个文件里同时
    # 有 ask_knowledge_base / search_knowledge 这类回答路径，所以它单列出来
    # 走下面的按函数粒度检查，不能整文件放过。
    "kbase_mcp/server.py",
    # T12 运营看板可能只读展示"这条归因已提取过标问"：展示不是回答路径
    "kbase/qa_stats.py",
    "kbase/api/routes/admin.py",
}

# kbase_mcp/server.py 里允许出现标问库标识的函数（T14 的提交工具）。
# 回答路径的工具（ask_knowledge_base / search_knowledge 等）出现在这里就是红线破了。
_MCP_SUBMIT_FUNCS = {"submit_standard_answer_impl", "submit_standard_answer"}

# **数据面**：产生召回/答案或对外输出的代码。红线那条路径（相似度命中 →
# 绕过检索 → 直接返回答案）只能长在这里，所以这些文件必须完全不知道标问库。
_DATA_PLANE = (
    "kbase/rag/",
    "kbase/ingest/",
    "kbase/retrieval_strategy.py",
    "kbase/conversations.py",
    "kbase/feedback.py",
    "kbase/api/routes/query.py",
    "kbase/api/routes/openai_compat.py",
    "kbase/api/routes/share.py",
    "kbase/api/routes/feishu_bot.py",
    "kbase/api/routes/evals.py",
    "kbase/feishu_bot.py",
)


def _production_py_files():
    """全仓 .py（kbase / kbase_mcp / scripts / eval / 根目录……），跳过
    构建产物与前端目录——回答路径在服务端，前端只能调端点。"""
    out = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if set(p.relative_to(ROOT).parts) & _SKIP_DIRS:
            continue
        if rel.startswith("tests/") or p.name.startswith("test_"):
            continue
        out.append((rel, p))
    return out


def _standard_answer_refs():
    """{相对路径: 命中的标识集合}——全仓 grep 的结果。"""
    refs = {}
    for rel, p in _production_py_files():
        found = {m.group(0) for m in _PATTERN.finditer(
            p.read_text(encoding="utf-8", errors="ignore"))}
        if found:
            refs[rel] = found
    return refs


def _mcp_functions_referencing_standard_answers():
    """kbase_mcp/server.py 里"提到标问库"的函数名集合（模块级代码记 "<module>"）。

    这个文件不能整文件放过：它既有 T14 的提交工具（管理面，合法），也有
    ask_knowledge_base / search_knowledge 这类**回答路径**。所以按函数粒度切，
    且只算函数"自己的行"（嵌套工具的定义连同装饰器 description 归它自己），
    否则 build_mcp 会因为包着提交工具而误报。这样才钉得住"MCP 里不存在
    '先查标问库再回答'的工具"。
    """
    import ast
    path = ROOT / "kbase_mcp" / "server.py"
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    spans = []            # (函数名, 起始行, 结束行)；装饰器行算在函数自己头上
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            spans.append((node.name, start, node.end_lineno))
    hits = set()
    for name, start, end in spans:
        own = [n for n in range(start, end + 1)
               if not any(s <= n <= e and (s, e) != (start, end)
                          for _n, s, e in spans)]
        if _PATTERN.search("\n".join(lines[n - 1] for n in own)):
            hits.add(name)
    covered = {n for _n, s, e in spans for n in range(s, e + 1)}
    outside = "\n".join(line for n, line in enumerate(lines, start=1)
                        if n not in covered)
    if _PATTERN.search(outside):        # 模块级 import / 常量 / 顶层调用
        hits.add("<module>")
    return hits


def test_no_direct_return_path_in_answer_pipeline():
    """**红线闸门**：数据面（检索/问答/生成/对外入口/MCP 回答工具）里不许出现
    标问库的任何标识——"相似度命中就绕过检索直接返回答案"只能从那里长出来。
    """
    refs = _standard_answer_refs()
    leaked = {rel: sorted(names) for rel, names in refs.items()
              if any(rel.startswith(p) for p in _DATA_PLANE)}
    assert leaked == {}, (
        "红线被打破：检索/问答/生成/对外入口引用了标问库——审核通过的标问只能"
        f"回灌评测集或走正常摄取管道：{leaked}")

    # MCP 的 answer 路径（ask_knowledge_base / search_knowledge 等）同样不许碰
    # 标问库：整文件放行会漏掉这条最诱人的"先查标问再回答"捷径
    mcp_hits = _mcp_functions_referencing_standard_answers()
    assert mcp_hits <= _MCP_SUBMIT_FUNCS, (
        "红线被打破：MCP 的非提交函数引用了标问库（只允许 T14 的提交工具）："
        f"{sorted(mcp_hits - _MCP_SUBMIT_FUNCS)}")

    # similar_questions 只在策展侧（录入/审核/回灌/提交）出现：它是"相似问法
    # 命中"唯一的数据来源，出现在别处就说明有人在做相似度直达
    curation = {"kbase/models.py", "kbase/standard_answers.py",
                "kbase/api/schemas.py", "kbase/api/routes/standard_answers.py",
                "kbase_mcp/server.py"}
    stray = {rel for rel, names in refs.items() if "similar_questions" in names} - curation
    assert stray == set(), f"similar_questions 只允许策展侧使用，出现在：{sorted(stray)}"


def test_standard_answer_references_are_all_registered():
    """第二道闸门：标问库的引用面必须完全已知。新增一个引用方就得回到这个
    白名单前回答一句"它是不是回答路径"——这正是红线要的复核点。"""
    refs = _standard_answer_refs()
    unknown = sorted(set(refs) - _MANAGEMENT_PLANE)
    assert unknown == [], (
        f"标问库出现未登记的引用方 {unknown}：先确认它不是回答路径再登记")
    # 非空校验：扫描本身必须真的扫到了东西（否则上面两条断言是空转的）
    assert {"kbase/models.py", "kbase/standard_answers.py",
            "kbase/api/routes/standard_answers.py"} <= set(refs)
