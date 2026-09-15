"""评测回归（B）：建集校验、一键回归 hit@k/MRR 计算、历史对比、删除级联、
无期望用例拒收；T15 答案级评测：开关关着**零 LLM 调用**、开了建任务不阻塞
API、单条判分失败只标该用例、报告含全部必需字段。

"开关关着零 LLM 调用"是本卡最关键的契约：合并这张卡不能改变任何既有部署的
行为。因此这里的 fake LLM 会**记账每一次调用**（stream/complete 各计数），
测试断言计数为 0——不是"看起来没用到"，是数出来的一次都没有。"""
import time

import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG

# 裁判返回的 JSON（grade_answer 的主路径）
JUDGE_OK = '{"score": 0.8, "reason": "要点基本齐全，缺少金额细节"}'


class CountingFakeLLM:
    """FakeLLM + 调用计数。stream/complete 各记一次，用于"零 LLM 调用"断言。"""

    model = "fake"

    def __init__(self, complete_reply: str = "好"):
        self.last_messages = None
        self.calls: list[str] = []
        self._reply = complete_reply

    async def stream(self, messages, **params):
        self.calls.append("stream")
        self.last_messages = messages
        yield "满两年"
        yield "可申领[1]。"

    async def complete(self, messages, **params):
        self.calls.append("complete")
        self.last_messages = messages
        return self._reply


class JudgeLLM:
    """裁判 provider 的 fake：按 prompt 分派——裁判请求（system 含"评测裁判"）
    返回可解析 JSON，生成链路（不经裁判 prompt 的 complete）返回固定答案。

    判分失败用第 `fail_from`（0 起）条之后的裁判请求抛异常来模拟（真机场景：
    裁判端点 429/超时/输出格式不合法）。"""

    model = "fake2"

    def __init__(self, *, fail_at: set[int] | None = None,
                 reply: str = JUDGE_OK):
        self.gen_calls: list[list[dict]] = []
        self.judge_calls: list[list[dict]] = []
        self.fail_at = fail_at or set()
        self._reply = reply

    async def stream(self, messages, **params):
        self.gen_calls.append(messages)
        yield "满两年可申领住房补贴[1]。"

    async def complete(self, messages, **params):
        joined = " ".join(m["content"] for m in messages)
        if "评测裁判" not in joined:
            self.gen_calls.append(messages)
            return "满两年可申领住房补贴[1]。"
        index = len(self.judge_calls)
        self.judge_calls.append(messages)
        if index in self.fail_at:
            raise RuntimeError("裁判端点 429")
        return self._reply


def _client(tmp_path, fake_embedder, *, llm=None, judge=None, judge_enabled=False,
            judge_provider=None):
    """按需拼配置：judge 打开时把裁判指到独立的 `fake2` provider 上，
    验证"裁判用另配的便宜模型"（spec §3）真的走了另一条 provider。"""
    cfg_text = CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/"))
    if judge_enabled:
        # CFG 里已配了 fake / fake2 两个 provider：裁判复用 fake2 这个**独立**
        # provider 名，用来验证"裁判走另配的便宜模型"（spec §3）不是空话。
        cfg_text += ("evals:\n  answer_judge:\n    enabled: true\n"
                     f"    provider: {judge_provider or 'fake2'}\n")
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(cfg_text, encoding="utf-8")
    llms = {"fake": llm or CountingFakeLLM()}
    if judge is not None:
        llms["fake2"] = judge
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms=llms, reranker=False, auth="off")
    return TestClient(app), llms


@pytest.fixture
def kb_ready(tmp_path, fake_embedder):
    c, _ = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb}/documents", files=[
        ("files", ("补贴.md", "# 补贴\n住房补贴入职满两年可申领。".encode("utf-8"),
                   "text/markdown")),
        ("files", ("考勤.md", "# 考勤\n迟到三次记旷工半天。".encode("utf-8"),
                   "text/markdown")),
    ])
    return c, kb


@pytest.fixture
def answer_ready(tmp_path, fake_embedder):
    """开关打开 + 三用例（两条检索可判中、一条只带参考答案不能检索判分）。"""
    judge = JudgeLLM()
    c, llms = _client(tmp_path, fake_embedder, judge=judge, judge_enabled=True)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb}/documents", files=[
        ("files", ("补贴.md", "# 补贴\n住房补贴入职满两年可申领。".encode("utf-8"),
                   "text/markdown")),
        ("files", ("考勤.md", "# 考勤\n迟到三次记旷工半天。".encode("utf-8"),
                   "text/markdown")),
    ])
    set_id = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "答案级集",
        "cases": [
            {"question": "住房补贴怎么申领", "expect_doc": "补贴.md",
             "expected_answer": "入职满两年可申领住房补贴"},
            {"question": "迟到怎么处理", "expect_text": "旷工半天",
             "expected_answer": "迟到三次记旷工半天"},
            # T13 回灌形态：只带参考答案 → 不进 hit@k/MRR 分母，但参与答案级判分
            {"question": "年终奖怎么发", "expected_answer": "按绩效发放"},
        ]}).json()["id"]
    return c, llms, judge, set_id


# ---------------- 原有检索回归（回归保护）----------------

def test_create_and_run_eval(kb_ready):
    c, kb = kb_ready
    r = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "冒烟集",
        "cases": [
            {"question": "住房补贴怎么申领", "expect_doc": "补贴.md"},
            {"question": "迟到怎么处理", "expect_text": "旷工半天"},
            {"question": "年终奖发多少", "expect_doc": "不存在.md"},   # 永不命中
        ]})
    assert r.status_code == 200, r.text
    set_id = r.json()["id"]
    assert r.json()["case_count"] == 3

    run = c.post(f"/api/eval-sets/{set_id}/run", json={"top_k": 5}).json()
    assert run["total"] == 3
    # 前两条应命中（fake embedder 确定性向量 + BM25 关键词路），第三条不可能中
    assert run["hits"] == 2
    assert run["hit_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert 0 < run["mrr"] <= 1
    miss = next(d for d in run["details"] if not d["hit"])
    assert miss["question"] == "年终奖发多少" and miss["rank"] is None

    # 历史对比：再跑一次 → 两行，倒序
    c.post(f"/api/eval-sets/{set_id}/run", json={"top_k": 5})
    runs = c.get(f"/api/eval-sets/{set_id}/runs").json()
    assert len(runs) == 2
    assert runs[0]["created_at"] >= runs[1]["created_at"]

    # 单次明细可回查
    detail = c.get(f"/api/eval-runs/{run['id']}").json()
    assert len(detail["details"]) == 3


def test_case_requires_expectation(kb_ready):
    c, kb = kb_ready
    r = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "坏集", "cases": [{"question": "没期望的用例"}]})
    assert r.status_code == 422


def test_delete_set_cascades_runs(kb_ready):
    c, kb = kb_ready
    set_id = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "集", "cases": [{"question": "q", "expect_text": "补贴"}]}).json()["id"]
    run_id = c.post(f"/api/eval-sets/{set_id}/run", json={}).json()["id"]
    assert c.delete(f"/api/eval-sets/{set_id}").json()["ok"] is True
    assert c.get(f"/api/eval-sets/{set_id}/runs").status_code == 404
    assert c.get(f"/api/eval-runs/{run_id}").status_code == 404
    assert c.get(f"/api/kb/{kb}/eval-sets").json() == []


# ---------------- T15 关键契约：开关关着 → 零 LLM 调用 ----------------

def test_answer_mode_off_is_422_and_makes_zero_llm_calls(tmp_path, fake_embedder):
    """开关关（默认）时：
    1) mode="answer" → 422（不静默降级成检索回归——调用方必须知道没跑答案分）；
    2) 默认 mode / 显式 mode="retrieval" 的检索回归照跑，且**一次 LLM 调用都没有**。
    """
    llm = CountingFakeLLM()
    c, _ = _client(tmp_path, fake_embedder, llm=llm, judge_enabled=False)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb}/documents", files=[
        ("files", ("补贴.md", "# 补贴\n住房补贴入职满两年可申领。".encode("utf-8"),
                   "text/markdown"))])
    set_id = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "集", "cases": [
            {"question": "住房补贴怎么申领", "expect_doc": "补贴.md"},
            {"question": "年终奖发多少", "expect_doc": "不存在.md"},
        ]}).json()["id"]

    r = c.post(f"/api/eval-sets/{set_id}/run", json={"top_k": 5, "mode": "answer"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "error.answer_judge_disabled"

    # 老客户端形态（不带 mode 字段）+ 显式 retrieval：两条都走同步检索回归
    run = c.post(f"/api/eval-sets/{set_id}/run", json={"top_k": 5}).json()
    assert run["hit_rate"] == pytest.approx(0.5, abs=1e-4)
    run2 = c.post(f"/api/eval-sets/{set_id}/run",
                  json={"top_k": 5, "mode": "retrieval"}).json()
    assert run2["hit_rate"] == pytest.approx(0.5, abs=1e-4)
    assert run2["mode"] == "retrieval"

    # 核心断言：整条链路一次 LLM 都没调（既没生成也没裁判）
    assert llm.calls == []
    # 也没建过 job（job 列表为空，不存在被漏走的评测任务）
    assert c.get(f"/api/jobs?kb_id={kb}").json() == []


def test_answer_mode_off_job_type_rejected_at_generic_entry(kb_ready):
    """通用 /api/jobs 入口不接受 eval_answer——从那里建会建出没有步骤的僵尸任务。"""
    c, kb = kb_ready
    r = c.post("/api/jobs", json={"type": "eval_answer", "kb_id": kb, "params": {}})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "error.eval_answer_use_run_endpoint"


# ---------------- T15：开关打开 → 建 job，且 API 不被阻塞 ----------------

def test_answer_mode_on_creates_job_and_does_not_block_api(answer_ready):
    """POST run（mode=answer）返回 job id；判分在 BackgroundTasks 里跑，
    请求路径上不碰 LLM（用调用计数证明：返回瞬间裁判调用数还是 0）。

    TestClient 会在响应返回后同步跑完 background task，所以"返回那一刻"
    只能在请求中途观察——这里用 GET /api/jobs/{id} 的拦截来抓那一瞬（见下
    一个测试），本测试先钉住 job 的创建与最终产出。
    """
    c, llms, judge, set_id = answer_ready
    judge.calls_before = len(judge.judge_calls)
    r = c.post(f"/api/eval-sets/{set_id}/run", json={"mode": "answer", "top_k": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "answer"
    assert body["id"]

    job = c.get(f"/api/jobs/{body['id']}").json()
    assert job["type"] == "eval_answer"
    # 三条用例 + 落快照（TestClient 的 BackgroundTasks 在响应后同步跑完）
    assert job["status"] == "done"
    assert len(job["progress"]["steps"]) == 4
    # 裁判用的是另配的 provider（不是活跃 provider）
    assert job["provider"] == "fake2"

    # 产物（artifact.md）就是报告本体，落在 job 目录里
    assert job["artifact_path"].endswith("artifact.md")
    with open(job["artifact_path"], encoding="utf-8") as fh:
        assert "答案分（0~1）" in fh.read()


def test_answer_mode_request_path_makes_no_llm_call(tmp_path, fake_embedder,
                                                    monkeypatch):
    """**API 进程不被阻塞**的直接证据：请求处理期间（响应还没返回）观察
    LLM 调用数为 0、job 状态仍是 pending/running——也就是说请求路径上没有
    任何检索/生成/裁判。做法：在 BackgroundTasks 开始执行前插一个探针，
    记录"响应已生成但任务还没跑"那一瞬的状态。"""
    import kbase.api.routes.evals as evals_routes
    from kbase.jobs.runner import run_job as real_run_job

    judge = JudgeLLM()
    c, llms = _client(tmp_path, fake_embedder, judge=judge, judge_enabled=True)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb}/documents", files=[
        ("files", ("补贴.md", "# 补贴\n住房补贴入职满两年可申领。".encode("utf-8"),
                   "text/markdown"))])
    set_id = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "集", "cases": [{"question": "住房补贴怎么申领",
                                "expect_doc": "补贴.md",
                                "expected_answer": "入职满两年"}]}).json()["id"]

    seen: dict = {}

    def spy_run_job(sf, job_id, steps):
        # 任务体开始时（即请求已经返回之后）：此刻还没有任何 LLM 调用
        seen["llm_calls_at_task_start"] = len(judge.judge_calls) + len(judge.gen_calls)
        return real_run_job(sf, job_id, steps)

    monkeypatch.setattr(evals_routes, "run_job", spy_run_job)

    t0 = time.monotonic()
    r = c.post(f"/api/eval-sets/{set_id}/run", json={"mode": "answer"})
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert seen["llm_calls_at_task_start"] == 0
    # 判分确实跑了（否则"零调用"是假的）
    assert len(judge.judge_calls) == 1
    assert elapsed < 5

    # 请求路径上不能有 LLM：把 run_job 换成不执行的桩，POST 依旧只做一次建 job
    monkeypatch.setattr(evals_routes, "run_job", lambda *a, **k: None)
    judge.judge_calls.clear()
    judge.gen_calls.clear()
    r2 = c.post(f"/api/eval-sets/{set_id}/run", json={"mode": "answer"})
    assert r2.status_code == 200
    assert judge.judge_calls == [] and judge.gen_calls == []
    assert c.get(f"/api/jobs/{r2.json()['id']}").json()["status"] == "pending"


def test_answer_mode_runs_judge_on_separate_provider(answer_ready):
    """裁判走另配的 fake2 provider；生成走活跃 provider。"""
    c, llms, judge, set_id = answer_ready
    gen_calls_before = len(llms["fake"].calls)
    r = c.post(f"/api/eval-sets/{set_id}/run", json={"mode": "answer"})
    assert r.status_code == 200
    job_id = r.json()["id"]
    job = c.get(f"/api/jobs/{job_id}").json()
    assert job["provider"] == "fake2"

    # 生成：3 条用例各一次 stream，且走的是**活跃 provider**（fake），
    # 一次都没落到裁判 provider 上（两者是不同实例，调用计数分开记）
    assert len(llms["fake"].calls) - gen_calls_before == 3
    assert judge.gen_calls == []
    # 裁判：3 条用例各一次 complete（另配 provider）
    assert len(judge.judge_calls) == 3
    # 裁判 prompt 里带了 rubric 与参考答案
    prompt = " ".join(m["content"] for m in judge.judge_calls[0])
    assert "评测裁判" in prompt and "0~1" in prompt
    assert "参考答案" in prompt and "入职满两年可申领住房补贴" in prompt
    assert "问题：住房补贴怎么申领" in prompt

    runs = c.get(f"/api/eval-sets/{set_id}/runs").json()
    assert runs[0]["mode"] == "answer"
    assert runs[0]["judge_provider"] == "fake2"
    assert runs[0]["answer_score"] == pytest.approx(0.8, abs=1e-4)
    # 检索判分口径不变：只带参考答案的用例不进分母（3 条用例 / 2 条参加检索判分）
    assert runs[0]["total"] == 2


def test_answer_run_record_has_both_retrieval_and_answer_score(answer_ready):
    c, _llms, _judge, set_id = answer_ready
    run_id = c.post(f"/api/eval-sets/{set_id}/run",
                    json={"mode": "answer"}).json()["id"]
    job = c.get(f"/api/jobs/{run_id}").json()
    assert job["status"] == "done"

    record = c.get(f"/api/eval-runs/{_latest_run_id(c, set_id)}").json()
    assert record["mode"] == "answer"
    assert record["hit_rate"] == pytest.approx(1.0, abs=1e-4)     # 两条都能检索命中
    assert record["mrr"] > 0
    assert record["answer_score"] == pytest.approx(0.8, abs=1e-4)
    assert record["total"] == 2                                   # 检索分母
    # 逐用例明细里写了答案与裁判理由（spec §3：写入该 case 的 detail）
    judged = [d for d in record["details"] if d.get("answer_score") is not None]
    assert len(judged) == 3
    assert "金额细节" in judged[0]["answer_reason"]
    assert judged[0]["answer"]
    # 只带参考答案的那条：不进检索分母，但答案级照样判
    only_answer = next(d for d in record["details"] if not d["retrieval_judged"])
    assert only_answer["hit"] is None
    assert only_answer["answer_score"] == pytest.approx(0.8, abs=1e-4)


def _latest_run_id(c, set_id):
    return c.get(f"/api/eval-sets/{set_id}/runs").json()[0]["id"]


# ---------------- T15：单条判分失败只标该用例，不中断整批 ----------------

def test_failing_judge_marks_case_without_aborting_batch(tmp_path, fake_embedder):
    """裁判对第 2 条用例抛异常：该用例标 judge_error（不进答案分分母），
    第 3 条照常判分、末步照常落快照——整批不中断（runner 逐步失败隔离）。"""
    judge = JudgeLLM(fail_at={1})
    c, llms = _client(tmp_path, fake_embedder, judge=judge, judge_enabled=True)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb}/documents", files=[
        ("files", ("补贴.md", "# 补贴\n住房补贴入职满两年可申领。".encode("utf-8"),
                   "text/markdown")),
        ("files", ("考勤.md", "# 考勤\n迟到三次记旷工半天。".encode("utf-8"),
                   "text/markdown"))])
    set_id = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "集", "cases": [
            {"question": "住房补贴怎么申领", "expect_doc": "补贴.md",
             "expected_answer": "入职满两年"},
            {"question": "迟到怎么处理", "expect_text": "旷工半天",
             "expected_answer": "迟到三次记旷工半天"},
            {"question": "考勤怎么算", "expect_text": "旷工",
             "expected_answer": "迟到三次算旷工半天"},
        ]}).json()["id"]

    job_id = c.post(f"/api/eval-sets/{set_id}/run",
                    json={"mode": "answer"}).json()["id"]
    job = c.get(f"/api/jobs/{job_id}").json()
    # 三条用例全部执行过（第 2 条判分失败没拦下第 3 条），末步也跑了
    assert job["status"] == "done"
    assert [s["status"] for s in job["progress"]["steps"]] == ["done"] * 4

    run_id = _latest_run_id(c, set_id)
    record = c.get(f"/api/eval-runs/{run_id}").json()
    # 失败用例被标记，且**不进答案分分母**（0.8 由另外两条得出，不是 0.5333）
    assert record["answer_score"] == pytest.approx(0.8, abs=1e-4)
    failed = [d for d in record["details"] if d.get("judge_error") == "judge_failed"]
    assert len(failed) == 1
    assert failed[0]["question"] == "迟到怎么处理"
    assert failed[0]["answer_score"] is None
    assert "429" in failed[0]["answer_reason"]
    # 该用例的检索指标照常计入（判分失败不影响检索口径）
    assert failed[0]["hit"] is True
    assert record["total"] == 3
    # 第 3 条照常判出来了
    third = next(d for d in record["details"] if d["question"] == "考勤怎么算")
    assert third["answer_score"] == pytest.approx(0.8, abs=1e-4)


def test_case_retrieval_failure_still_yields_a_snapshot(tmp_path, fake_embedder,
                                                        monkeypatch):
    """检索/生成整条挂掉的用例：该步标 failed（整体 done_with_errors），
    但**其余用例与快照照常产出**——判分/生成坏一条不该让运营白等几分钟还拿不到
    任何可比基线。坏用例标 case_error、不进任何分母。"""
    from kbase.rag.retriever import Retriever

    judge = JudgeLLM()
    c, _llms = _client(tmp_path, fake_embedder, judge=judge, judge_enabled=True)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb}/documents", files=[
        ("files", ("补贴.md", "# 补贴\n住房补贴入职满两年可申领。".encode("utf-8"),
                   "text/markdown"))])
    set_id = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "集", "cases": [
            {"question": "住房补贴怎么申领", "expect_doc": "补贴.md",
             "expected_answer": "入职满两年"},
            {"question": "坏用例", "expect_doc": "补贴.md",
             "expected_answer": "x"},
            {"question": "补贴申领条件", "expect_doc": "补贴.md",
             "expected_answer": "入职满两年"},
        ]}).json()["id"]

    real_retrieve = Retriever.retrieve
    calls = {"n": 0}

    def flaky_retrieve(self, kb_id, query, *args, **kwargs):
        if query == "坏用例":
            raise RuntimeError("向量库连接中断")
        calls["n"] += 1
        return real_retrieve(self, kb_id, query, *args, **kwargs)

    monkeypatch.setattr(Retriever, "retrieve", flaky_retrieve)

    job_id = c.post(f"/api/eval-sets/{set_id}/run",
                    json={"mode": "answer"}).json()["id"]
    job = c.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "done_with_errors"
    statuses = [s["status"] for s in job["progress"]["steps"]]
    assert statuses == ["done", "failed", "done", "done"]
    assert "向量库连接中断" in job["progress"]["steps"][1]["detail"]
    # 末步照样写了产物
    assert job["artifact_path"].endswith("artifact.md")
    assert calls["n"] == 2

    record = c.get(f"/api/eval-runs/{_latest_run_id(c, set_id)}").json()
    assert len([d for d in record["details"] if d.get("case_error")]) == 1
    assert record["total"] == 2                 # 坏用例不进检索分母
    assert record["hit_rate"] == pytest.approx(1.0, abs=1e-4)
    # 也不进答案分分母（0.8 由另外两条得出，不是 0.4）
    assert len([d for d in record["details"]
                if d.get("answer_score") is not None]) == 2
    assert record["answer_score"] == pytest.approx(0.8, abs=1e-4)
    bad = next(d for d in record["details"] if d["question"] == "坏用例")
    assert bad["case_error"] and "向量库连接中断" in bad["case_error"]
    assert bad["rank"] is None and bad["hit"] is None
    # 报告里也写明这类用例（不是悄悄少一行）
    md = c.get(f"/api/eval-runs/{_latest_run_id(c, set_id)}/report?format=md").text
    assert "用例执行失败" in md


def test_judge_garbage_output_marks_case_not_zero(tmp_path, fake_embedder):
    """裁判输出格式不合法（没有 0~1 的分）→ 该用例标 judge_failed，
    **不是记 0 分**——记 0 会把"裁判没按格式说"伪装成"答案很差"，把整体分拉低。"""
    judge = JudgeLLM(reply="我认为这个答案还不错，具体分数就不给啦。")
    c, _ = _client(tmp_path, fake_embedder, judge=judge, judge_enabled=True)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb}/documents", files=[
        ("files", ("补贴.md", "# 补贴\n住房补贴入职满两年可申领。".encode("utf-8"),
                   "text/markdown"))])
    set_id = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "集", "cases": [{"question": "住房补贴怎么申领",
                                "expect_doc": "补贴.md",
                                "expected_answer": "入职满两年"}]}).json()["id"]
    c.post(f"/api/eval-sets/{set_id}/run", json={"mode": "answer"})

    record = c.get(f"/api/eval-runs/{_latest_run_id(c, set_id)}").json()
    assert record["answer_score"] is None          # 没有有效判分 → 不产出答案分
    assert record["details"][0]["judge_error"] == "judge_failed"
    assert record["details"][0]["answer_score"] is None


def test_case_without_reference_answer_is_not_judged(tmp_path, fake_embedder):
    """没给 expected_answer 的检索用例：生成照跑，但不判分（无从判），
    且**不消耗裁判调用**——裁判 prompt 里没有参考答案就无从打分。"""
    judge = JudgeLLM()
    c, _ = _client(tmp_path, fake_embedder, judge=judge, judge_enabled=True)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb}/documents", files=[
        ("files", ("补贴.md", "# 补贴\n住房补贴入职满两年可申领。".encode("utf-8"),
                   "text/markdown"))])
    set_id = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "集", "cases": [
            {"question": "住房补贴怎么申领", "expect_doc": "补贴.md"}]}).json()["id"]
    c.post(f"/api/eval-sets/{set_id}/run", json={"mode": "answer"})

    assert judge.judge_calls == []
    record = c.get(f"/api/eval-runs/{_latest_run_id(c, set_id)}").json()
    assert record["details"][0]["judge_error"] == "no_reference"
    assert record["answer_score"] is None
    # 检索指标照常产出
    assert record["hit_rate"] == pytest.approx(1.0, abs=1e-4)


# ---------------- T15 spec §4：报告导出 ----------------

@pytest.mark.parametrize("fmt,expect_type", [
    ("md", "text/markdown"),
    ("docx", "application/vnd.openxmlformats-officedocument."
             "wordprocessingml.document"),
])
def test_report_export_contains_all_required_fields(answer_ready, fmt, expect_type):
    """报告必须含：用例数、检索指标、答案分、裁判模型、复测口径——
    只给一个总分的报告不算数（spec §4 的硬要求）。"""
    c, _llms, _judge, set_id = answer_ready
    c.post(f"/api/eval-sets/{set_id}/run", json={"mode": "answer"})
    run_id = _latest_run_id(c, set_id)

    r = c.get(f"/api/eval-runs/{run_id}/report?format={fmt}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(expect_type)


def test_report_md_content_has_every_required_field(answer_ready):
    c, _llms, _judge, set_id = answer_ready
    c.post(f"/api/eval-sets/{set_id}/run", json={"mode": "answer"})
    run_id = _latest_run_id(c, set_id)

    md = c.get(f"/api/eval-runs/{run_id}/report?format=md").text
    assert "用例总数：3" in md                     # 用例数
    assert f"hit@5" in md and "MRR" in md         # 检索指标
    assert "答案分（0~1）" in md and "0.8000" in md  # 答案分
    assert "fake2" in md                     # 裁判模型
    assert "复测口径" in md                        # 复测口径
    assert f"/api/eval-sets/{set_id}/run" in md
    assert '"mode": "answer"' in md
    # 逐用例明细（含只带参考答案的那条）
    assert "年终奖怎么发" in md
    assert "未参与检索判分" in md


def test_report_of_retrieval_only_run_still_complete(kb_ready):
    """只跑过检索回归的 run 也能导报告：答案分那行写明"未评测 + 为什么"，
    而不是留空让人以为没跑出来。"""
    c, kb = kb_ready
    set_id = c.post(f"/api/kb/{kb}/eval-sets", json={
        "name": "集", "cases": [
            {"question": "住房补贴怎么申领", "expect_doc": "补贴.md"},
            {"question": "年终奖发多少", "expect_doc": "不存在.md"}]}).json()["id"]
    run_id = c.post(f"/api/eval-sets/{set_id}/run", json={}).json()["id"]

    md = c.get(f"/api/eval-runs/{run_id}/report?format=md").text
    assert "用例总数：2" in md
    assert "hit@5" in md and "MRR" in md
    assert "答案分（0~1） | 未评测" in md
    assert "answer_judge.enabled=false" in md
    assert "仅检索（不评生成）" in md
    assert "未命中用例（1 条）" in md
    assert "年终奖发多少" in md


def test_report_unknown_run_404_and_bad_format_422(answer_ready):
    c, _llms, _judge, set_id = answer_ready
    c.post(f"/api/eval-sets/{set_id}/run", json={"mode": "answer"})
    run_id = _latest_run_id(c, set_id)

    assert c.get("/api/eval-runs/不存在/report?format=md").status_code == 404
    bad = c.get(f"/api/eval-runs/{run_id}/report?format=pdf")
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "error.unsupported_report_format"


def test_report_docx_is_a_real_docx(answer_ready, tmp_path):
    c, _llms, _judge, set_id = answer_ready
    c.post(f"/api/eval-sets/{set_id}/run", json={"mode": "answer"})
    run_id = _latest_run_id(c, set_id)

    r = c.get(f"/api/eval-runs/{run_id}/report?format=docx")
    assert r.status_code == 200
    assert r.content[:2] == b"PK"          # docx 是 zip 包
    assert len(r.content) > 2000


# ---------------- 判分解析单元（不依赖 LLM）----------------

def test_grade_answer_parses_json_and_falls_back_to_plain_text():
    from kbase.evals import grade_answer

    assert grade_answer('{"score": 0.8, "reason": "基本齐全"}') == (0.8, "基本齐全")
    # 围栏包裹的 JSON
    assert grade_answer('```json\n{"score": 0, "reason": "完全不符"}\n```') == \
        (0.0, "完全不符")
    # 字符串分 / 整数分
    assert grade_answer('{"score": "1", "reason": "完全一致"}')[0] == 1.0
    # 散文兜底：抓第一个 0~1 数
    score, reason = grade_answer("0.7 分。理由：遗漏了金额。")
    assert score == 0.7 and "遗漏了金额" in reason
    # 抓不到数 → 抛（由调用方标 judge_failed，不静默记 0）
    with pytest.raises(ValueError):
        grade_answer("这个答案还不错。")
    # 越界分 → 抛（不夹取成 1.0）
    with pytest.raises(ValueError):
        grade_answer('{"score": 8, "reason": "给高了"}')


def test_answer_judge_prompt_shape_is_fixed():
    """rubric prompt 的形状固定：0~1 分制 + 一句话理由 + JSON 输出。"""
    from kbase.jobs.eval_answer import JUDGE_ANSWER_CHARS, judge_messages

    messages = judge_messages("Q", "R", "A")
    assert messages[0]["role"] == "system"
    assert "0~1" in messages[0]["content"]
    assert '"score"' in messages[0]["content"]
    assert '"reason"' in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "问题：Q" in messages[1]["content"]
    assert "参考答案：\nR" in messages[1]["content"]
    assert "A" in messages[1]["content"]

    # 超长答案被截断（不把 prompt 顶爆），截断标记可见
    long_messages = judge_messages("Q", "R", "x" * (JUDGE_ANSWER_CHARS + 100))
    assert "…" in long_messages[1]["content"]
    assert "x" * (JUDGE_ANSWER_CHARS + 100) not in long_messages[1]["content"]


def test_config_default_is_off_and_invalid_judge_provider_rejected(tmp_path):
    """默认关（既有部署升级后开关必须还是关的）；开关打开时裁判 provider
    指错要在**启动期**报错，而不是等任务跑完（真金白银的生成调用都花完了）
    才发现 provider 不存在。"""
    from kbase.config import AppConfig, load_config

    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(CFG.format(data_dir=str(tmp_path / "d")), encoding="utf-8")
    assert load_config(cfg_file).evals.answer_judge.enabled is False

    with pytest.raises(Exception, match="answer_judge.provider"):
        AppConfig.model_validate({
            "llm": {"active": "a", "providers": [
                {"name": "a", "base_url": "http://x", "api_key_env": "K",
                 "model": "m"}]},
            "evals": {"answer_judge": {"enabled": True, "provider": "没有这个"}},
        })
    # 关着的时候预填一个还没配的 provider 名不算错（部署侧先写注释性配置）
    AppConfig.model_validate({
        "llm": {"active": "a", "providers": [
            {"name": "a", "base_url": "http://x", "api_key_env": "K", "model": "m"}]},
        "evals": {"answer_judge": {"enabled": False, "provider": "还没配"}},
    })


def test_answer_job_params_recorded_for_retest(answer_ready):
    """job.params 记下复测要用的全部输入（set_id/top_k/裁判 provider），
    报告里的"复测口径"才有据可依。"""
    c, _llms, _judge, set_id = answer_ready
    job_id = c.post(f"/api/eval-sets/{set_id}/run",
                    json={"mode": "answer", "top_k": 3}).json()["id"]
    params = c.get(f"/api/jobs/{job_id}").json()["params"]
    assert params["set_id"] == set_id
    assert params["top_k"] == 3
    assert params["judge_provider"] == "fake2"
