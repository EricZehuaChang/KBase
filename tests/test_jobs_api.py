"""jobs API 全链路测试（F5）：POST outline / POST jobs / GET jobs 列表与详情 /
GET artifact（md/docx）。复用 tests/test_api.py 的 CFG/MD/fake_embedder 模式。
"""
import json
import urllib.parse

from fastapi.testclient import TestClient

from kbase.api.main import create_app
from kbase.jobs.store import create_job

CFG = """
data_dir: {data_dir}
chunker: {{name: structure, chunk_size: 200, chunk_overlap: 0}}
llm:
  active: fake
  providers:
    - {{name: fake, base_url: 'http://x', api_key_env: FAKE_KEY, model: m}}
"""

MD = "# 补贴办法\n## 第一章 申领条件\n连续工作满两年可申领住房补贴。\n"


class ProgrammableFakeLLM:
    """按 prompt 内容分流返回：大纲请求（system prompt 含"大纲"）返回 JSON 数组；
    章节生成请求返回带 [1] 引用的正文；其余（如 test_connectivity）返回占位。"""
    model = "fake"

    def __init__(self):
        self.calls = []

    async def complete(self, messages, **params):
        self.calls.append(messages)
        joined = " ".join(m["content"] for m in messages)
        if "设计一份方案大纲" in joined:
            return json.dumps(
                [{"title": "第一章 背景", "brief": "说明背景"},
                 {"title": "第二章 依据", "brief": "说明政策依据"}],
                ensure_ascii=False)
        return "连续工作满两年可申领住房补贴[1]。"


def _client(tmp_path, fake_embedder, llm=None):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": llm or ProgrammableFakeLLM()}, reranker=False,
                     auth="off")
    return app, TestClient(app)


def _seed_kb_with_doc(c, tmp_path, fake_embedder):
    kb_id = c.post("/api/kb", json={"name": "彩排库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    return kb_id


# ---- POST /api/proposals/outline ----

def test_outline_endpoint_returns_sections(tmp_path, fake_embedder):
    app, c = _client(tmp_path, fake_embedder)
    kb_id = _seed_kb_with_doc(c, tmp_path, fake_embedder)

    r = c.post("/api/proposals/outline", json={
        "kb_id": kb_id, "topic": "住房保障方案", "requirements": "依据现行政策"})
    assert r.status_code == 200
    sections = r.json()
    assert isinstance(sections, list)
    assert sections[0]["title"] == "第一章 背景"
    assert sections[1]["title"] == "第二章 依据"


def test_outline_endpoint_unknown_provider_404(tmp_path, fake_embedder):
    app, c = _client(tmp_path, fake_embedder)
    kb_id = _seed_kb_with_doc(c, tmp_path, fake_embedder)

    r = c.post("/api/proposals/outline", json={
        "kb_id": kb_id, "topic": "住房保障方案", "requirements": "",
        "provider": "不存在的provider"})
    assert r.status_code == 404


def test_outline_endpoint_bad_json_502(tmp_path, fake_embedder):
    class BadLLM:
        model = "fake"

        async def complete(self, messages, **params):
            return "这不是JSON也没有中括号"

    app, c = _client(tmp_path, fake_embedder, llm=BadLLM())
    kb_id = _seed_kb_with_doc(c, tmp_path, fake_embedder)

    r = c.post("/api/proposals/outline", json={
        "kb_id": kb_id, "topic": "住房保障方案", "requirements": ""})
    assert r.status_code == 502
    assert "detail" in r.json()


# ---- POST /api/jobs + polling + artifact download：proposal 全链路 ----

def test_full_proposal_job_lifecycle_md_and_docx_artifact(tmp_path, fake_embedder):
    app, c = _client(tmp_path, fake_embedder)
    kb_id = _seed_kb_with_doc(c, tmp_path, fake_embedder)

    outline = c.post("/api/proposals/outline", json={
        "kb_id": kb_id, "topic": "住房保障方案", "requirements": "依据现行政策"}).json()

    r = c.post("/api/jobs", json={
        "type": "proposal", "kb_id": kb_id,
        "params": {"topic": "住房保障方案", "outline": outline}})
    assert r.status_code == 200
    job_id = r.json()["id"]

    # TestClient 的 BackgroundTasks 在响应返回前已同步跑完，直接查详情应为 done
    detail = c.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "done"
    assert detail["type"] == "proposal"

    # 列表也能看到该 job
    listing = c.get(f"/api/jobs?kb_id={kb_id}").json()
    assert any(j["id"] == job_id for j in listing)

    # md 产物
    r_md = c.get(f"/api/jobs/{job_id}/artifact?format=md")
    assert r_md.status_code == 200
    assert "引用文献" in r_md.text

    # docx 产物：首次请求按需转换
    r_docx = c.get(f"/api/jobs/{job_id}/artifact?format=docx")
    assert r_docx.status_code == 200
    assert r_docx.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    # 非 ASCII 文件名走 RFC 5987 百分号编码（filename*=utf-8''...）
    assert urllib.parse.quote("方案.docx") in r_docx.headers["content-disposition"]


# ---- POST /api/jobs + polling + artifact download：digest 全链路 ----

def test_full_digest_job_lifecycle_artifact(tmp_path, fake_embedder):
    app, c = _client(tmp_path, fake_embedder)
    kb_id = _seed_kb_with_doc(c, tmp_path, fake_embedder)

    r = c.post("/api/jobs", json={"type": "digest", "kb_id": kb_id, "params": {}})
    assert r.status_code == 200
    job_id = r.json()["id"]

    detail = c.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "done"
    assert detail["type"] == "digest"

    r_md = c.get(f"/api/jobs/{job_id}/artifact?format=md")
    assert r_md.status_code == 200
    assert "文档汇编" in r_md.text

    r_docx = c.get(f"/api/jobs/{job_id}/artifact?format=docx")
    assert r_docx.status_code == 200
    assert r_docx.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert urllib.parse.quote("汇编.docx") in r_docx.headers["content-disposition"]


# ---- 错误面 ----

def test_create_job_unknown_type_422(tmp_path, fake_embedder):
    app, c = _client(tmp_path, fake_embedder)
    kb_id = _seed_kb_with_doc(c, tmp_path, fake_embedder)

    r = c.post("/api/jobs", json={"type": "not-a-type", "kb_id": kb_id, "params": {}})
    assert r.status_code == 422


def test_create_proposal_job_missing_params_422(tmp_path, fake_embedder):
    app, c = _client(tmp_path, fake_embedder)
    kb_id = _seed_kb_with_doc(c, tmp_path, fake_embedder)

    r = c.post("/api/jobs", json={"type": "proposal", "kb_id": kb_id, "params": {}})
    assert r.status_code == 422


def test_create_job_unknown_kb_404(tmp_path, fake_embedder):
    app, c = _client(tmp_path, fake_embedder)

    r = c.post("/api/jobs", json={
        "type": "proposal", "kb_id": "不存在的kb",
        "params": {"topic": "t", "outline": [{"title": "a", "brief": "b"}]}})
    assert r.status_code == 404


def test_get_unknown_job_404(tmp_path, fake_embedder):
    app, c = _client(tmp_path, fake_embedder)

    r = c.get("/api/jobs/不存在的job")
    assert r.status_code == 404


def test_artifact_before_done_409(tmp_path, fake_embedder):
    app, c = _client(tmp_path, fake_embedder)
    kb_id = _seed_kb_with_doc(c, tmp_path, fake_embedder)

    from kbase.db import make_session_factory
    sf = make_session_factory(f"sqlite:///{tmp_path / 'data' / 'kbase.sqlite'}")
    job = create_job(sf, kb_id=kb_id, type="proposal",
                     params={"topic": "t", "outline": []}, provider=None)

    r = c.get(f"/api/jobs/{job['id']}/artifact?format=md")
    assert r.status_code == 409
