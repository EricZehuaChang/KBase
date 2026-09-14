import httpx
import pytest

from kbase_mcp.server import KBaseClient, ask_knowledge_base_impl, \
    get_chunk_impl, get_document_outline_impl, list_knowledge_bases_impl, \
    search_knowledge_impl, submit_standard_answer_impl
from tests.test_api import CFG, MD, FakeLLM

# T14：章节顺序刻意与标题字典序**相反**——"第二章"排在"第一章"前面，且"附录"
# 出现两次（同一 heading_path 的重复父块）。按 heading_path 排序会得到
# 第一章/第二章，只有按出现顺序建树才对得上。
OUTLINE_MD = """# 制度总览

## 第二章 补贴标准

每月 500 元。

## 第一章 申领条件

连续工作满两年。

## 附录

附则一。

## 附录

附则二。
"""

TABLE_MD = """# 补贴标准

| 城市 | 金额 |
| --- | --- |
| 北京 | 500 |
"""


@pytest.fixture
def kbase_app(tmp_path, fake_embedder):
    from kbase.api.main import create_app
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    return create_app(config_path=cfg, embedder=fake_embedder,
                      llms={"fake": FakeLLM()}, reranker=False, auth="off")


@pytest.fixture
async def client(kbase_app):
    transport = httpx.ASGITransport(app=kbase_app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://kbase.test") as c:
        yield KBaseClient(c)


async def _seed_kb(client: "KBaseClient") -> str:
    r = await client.http.post("/api/kb", json={"name": "政策库"})
    kb_id = r.json()["id"]
    files = {"files": ("补贴办法.md", MD.encode("utf-8"), "text/markdown")}
    await client.http.post(f"/api/kb/{kb_id}/documents", files=files)
    return kb_id


async def test_list_knowledge_bases(client):
    kb_id = await _seed_kb(client)
    out = await list_knowledge_bases_impl(client)
    assert any(k["id"] == kb_id and k["name"] == "政策库" for k in out)


async def test_search_knowledge(client):
    kb_id = await _seed_kb(client)
    q = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    out = await search_knowledge_impl(client, kb_id, q, top_k=3)
    assert out and "连续工作满两年" in out[0]["text"]
    assert set(out[0]) == {"doc_name", "heading_path", "text", "score"}


async def test_ask_knowledge_base(client):
    kb_id = await _seed_kb(client)
    q = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    out = await ask_knowledge_base_impl(client, kb_id, q)
    assert "满两年" in out["answer"]                     # FakeLLM 输出拼接
    assert out["citations"] and out["citations"][0]["doc_name"] == "补贴办法.md"


async def test_api_unreachable_clear_error():
    dead = httpx.AsyncClient(base_url="http://127.0.0.1:1")   # 无监听端口
    out = await list_knowledge_bases_impl(KBaseClient(dead))
    assert isinstance(out, dict) and "KBase 服务" in out["error"]
    await dead.aclose()


async def test_ask_knowledge_base_preserves_newline_in_multiline_token(
        kbase_app, monkeypatch):
    """sse-starlette 对含 \\n 的 token 事件会拆成多条 data 行（SSE 规范）；
    组装时必须按事件收集 dataLines 并以 \\n join，而不是 join("")，否则换行会丢失。
    用一个会 yield 含 \\n 的 token 的 FakeLLM 验证换行在最终 answer 中存活。"""

    class NewlineFakeLLM:
        model = "fake"

        async def stream(self, messages, **params):
            yield "第一行\n第二行"

        async def complete(self, messages, **params):
            return "好"

    from kbase.api.main import create_app
    monkeypatch.setattr("tests.test_mcp_tools.FakeLLM", NewlineFakeLLM)

    import tempfile
    from pathlib import Path
    tmp_path = Path(tempfile.mkdtemp())
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")

    import hashlib

    class _FakeEmbedder:
        dimension = 8

        def embed(self, texts):
            out = []
            for t in texts:
                h = int(hashlib.md5(t.encode("utf-8")).hexdigest(), 16)
                out.append([((h >> (i * 4)) % 100) / 100.0 for i in range(8)])
            return out

    app = create_app(config_path=cfg, embedder=_FakeEmbedder(),
                     llms={"fake": NewlineFakeLLM()}, reranker=False, auth="off")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://kbase.test") as http:
        c = KBaseClient(http)
        r = await http.post("/api/kb", json={"name": "政策库"})
        kb_id = r.json()["id"]
        files = {"files": ("补贴办法.md", MD.encode("utf-8"), "text/markdown")}
        await http.post(f"/api/kb/{kb_id}/documents", files=files)

        q = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
        out = await ask_knowledge_base_impl(c, kb_id, q)

    assert out["answer"] == "第一行\n第二行"


# ---- T14：新增的三个 MCP 工具（chunk 详情 / 文档大纲 / 提交标问）--------


async def _upload(client: "KBaseClient", kb_id: str, name: str,
                  text: str) -> tuple[str, list[dict]]:
    """上传一份文档，返回 (doc_id, 该文档的分块列表)。摄取是同步 bg task，
    响应返回时已完成（见 routes/kb.py 的 _ingest_batch 注释）。"""
    r = await client.http.post(f"/api/kb/{kb_id}/documents",
                               files={"files": (name, text.encode("utf-8"),
                                                "text/markdown")})
    assert r.status_code == 200, r.text
    docs = (await client.http.get(f"/api/kb/{kb_id}/documents")).json()
    doc_id = [d for d in docs if d["filename"] == name][0]["id"]
    items = (await client.http.get(
        f"/api/documents/{doc_id}/chunks")).json()["items"]
    return doc_id, items


async def test_get_chunk_returns_structure(client):
    """get_chunk 的返回结构：正文、标题链、页码、版式、所属文档；
    layout 必须是解析后的 JSON 对象（表格块），不是库里的字符串。"""
    kb_id = await _seed_kb(client)
    doc_id, items = await _upload(client, kb_id, "补贴标准.md", TABLE_MD)
    leaf = [c for c in items if c["is_leaf"]][0]

    out = await get_chunk_impl(client, leaf["id"])
    assert set(out) == {"doc_id", "doc_name", "heading_path", "text", "page",
                        "layout"}
    assert out["doc_id"] == doc_id and out["doc_name"] == "补贴标准.md"
    assert out["heading_path"] == leaf["heading_path"] == "补贴标准.md > 补贴标准"
    assert out["text"] == leaf["text"] and "北京" in out["text"]
    assert out["page"] is None                    # markdown 没有页码可定位
    assert out["layout"]["kind"] == "table"       # 版式 JSON 已解析


async def test_get_document_outline_tree_in_appearance_order(client):
    """get_document_outline 的返回结构：文件名前缀不入树，章节树按**出现
    顺序**（不是 heading_path 字典序）挂接，同 heading_path 的重复父块只留
    一个节点。"""
    kb_id = await _seed_kb(client)
    doc_id, _ = await _upload(client, kb_id, "制度总览.md", OUTLINE_MD)

    tree = await get_document_outline_impl(client, doc_id)
    assert [n["title"] for n in tree] == ["制度总览"]
    children = tree[0]["children"]
    assert [c["title"] for c in children] == ["第二章 补贴标准", "第一章 申领条件",
                                              "附录"]
    assert children[0]["heading_path"] == "制度总览.md > 制度总览 > 第二章 补贴标准"
    assert all(c["children"] == [] for c in children)
    # "附录"在文中出现两次（两个同 heading_path 的父块）→ 树里只有一个节点
    assert len({c["heading_path"] for c in children}) == len(children) == 3


async def test_new_tools_produce_structured_content_over_mcp_session(client):
    """Rider（与 test_mcp_transport 的 ask 用例同一诉求）：三个新工具的返回
    标注写成 list/dict 的 Union 才会生成 output_schema，走 STDIO 内存会话
    实调时 structuredContent 必须被真实填充（裸 `dict` 标注下恒为 None）。"""
    from mcp.shared.memory import create_connected_server_and_client_session

    from kbase_mcp.server import build_mcp
    kb_id = await _seed_kb(client)
    doc_id, items = await _upload(client, kb_id, "补贴标准.md", TABLE_MD)
    chunk_id = [c for c in items if c["is_leaf"]][0]["id"]

    fastmcp = build_mcp(client)
    async with create_connected_server_and_client_session(fastmcp) as session:
        names = {t.name for t in (await session.list_tools()).tools}
        assert names == {"list_knowledge_bases", "search_knowledge",
                         "ask_knowledge_base", "get_chunk",
                         "get_document_outline", "submit_standard_answer"}

        got = await session.call_tool("get_chunk", {"chunk_id": chunk_id})
        assert got.structuredContent["result"]["doc_name"] == "补贴标准.md"

        outline = await session.call_tool("get_document_outline",
                                          {"doc_id": doc_id})
        assert outline.structuredContent["result"][0]["title"] == "补贴标准"

        submitted = await session.call_tool(
            "submit_standard_answer",
            {"kb_id": kb_id, "question": "每月补贴多少", "answer": "500 元"})
        assert submitted.structuredContent["result"]["status"] == "pending_review"


async def test_scoped_key_out_of_scope_chunk_and_document_is_silently_empty(
        tmp_path, fake_embedder, monkeypatch):
    """受限 API Key 拿白名单外库的 chunk_id / doc_id：MCP 侧必须是**静默空集**
    （`{}` / `[]`），不是 404 报错——与 tests/test_apikey_scope.py 钉住的检索
    越权语义一致。两库都是公开库（无 grant），ACL 本来就放行，所以空集只可能
    来自 scope 这道闸门（排除假绿）；白名单内同一把 key 正常取到。"""
    from tests.test_auth import _client_on
    app, admin = _client_on(tmp_path, fake_embedder,
                            admin_password="adminpass123", monkeypatch=monkeypatch)
    admin.post("/api/auth/login", json={"username": "admin",
                                        "password": "adminpass123"})
    kb_ids: dict[str, str] = {}
    ids: dict[str, tuple[str, str]] = {}
    for name in ("允许库", "禁区库"):
        kb_id = admin.post("/api/kb", json={"name": name}).json()["id"]
        files = {"files": ("补贴办法.md", MD.encode("utf-8"), "text/markdown")}
        assert admin.post(f"/api/kb/{kb_id}/documents",
                          files=files).status_code == 200
        doc_id = admin.get(f"/api/kb/{kb_id}/documents").json()[0]["id"]
        chunk_id = admin.get(
            f"/api/documents/{doc_id}/chunks").json()["items"][0]["id"]
        kb_ids[name], ids[name] = kb_id, (doc_id, chunk_id)
    key = admin.post("/api/settings/api-keys",
                     json={"name": "scoped-mcp", "role": "viewer",
                           "scope_kb_ids": [kb_ids["允许库"]]}).json()["key"]

    doc_b, chunk_b = ids["禁区库"]
    doc_a, chunk_a = ids["允许库"]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://kbase.test",
            headers={"Authorization": f"Bearer {key}"}) as http:
        c = KBaseClient(http)
        # 端点层：200 + 空集（不是 404——不泄漏"存在但无权"）
        r_chunk = await http.get(f"/api/chunks/{chunk_b}")
        assert r_chunk.status_code == 200 and r_chunk.json() == {}
        r_outline = await http.get(f"/api/documents/{doc_b}/outline")
        assert r_outline.status_code == 200 and r_outline.json() == []
        # MCP 工具层：同样静默空集
        assert await get_chunk_impl(c, chunk_b) == {}
        assert await get_document_outline_impl(c, doc_b) == []
        # 对照：白名单内的库，同一把 key 正常取到
        assert (await get_chunk_impl(c, chunk_a))["doc_name"] == "补贴办法.md"
        assert await get_document_outline_impl(c, doc_a)

        # ACL 这道闸门没有被"静默空集"吃掉：把禁区库收紧（授权给别人）后，
        # 同一把 key 拿到的是 404，而不是 {} / []（不泄漏"存在但无权"）
        other = admin.post("/api/users", json={
            "username": "other", "role": "viewer",
            "password": "other-pw"}).json()
        admin.put(f"/api/kb/{kb_ids['禁区库']}/grants",
                  json={"user_ids": [other["id"]]})
        assert (await http.get(f"/api/chunks/{chunk_b}")).status_code == 404
        assert (await http.get(
            f"/api/documents/{doc_b}/outline")).status_code == 404


async def test_submit_standard_answer_only_enters_review_queue(client):
    """提交标问**只入审核队列**：落库为 pending_review，且审核前检索与问答
    逐字段不变（标问库红线：不存在"相似度命中就绕过检索直接返回答案"的路径，
    见 kbase/standard_answers.py 的模块注释）。"""
    kb_id = await _seed_kb(client)
    probe = "申领住房补贴"
    before = await search_knowledge_impl(client, kb_id, probe, top_k=10)

    question = "连续工作满几年可申领住房补贴？"
    answer = "标准答案标记：本条不得出现在检索结果里。"
    out = await submit_standard_answer_impl(
        client, kb_id, question, answer,
        similar_questions=["住房补贴怎么申领"], category="补贴")
    assert out["status"] == "pending_review"          # 创建恒为待审核
    assert out["kb_id"] == kb_id and out["answer"] == answer
    assert out["similar_questions"] == ["住房补贴怎么申领"]
    assert out["reviewed_by"] is None and out["reviewed_at"] is None

    after = await search_knowledge_impl(client, kb_id, probe, top_k=10)
    assert after == before                            # 检索结果逐字段不变
    assert all(answer not in b["text"] for b in after)
    qa = await ask_knowledge_base_impl(client, kb_id, question)
    assert answer not in qa["answer"]                 # 问答也不直接命中标问
    # 标问确实落库了（不是只回了个壳）——复核队列里能看到它
    rows = (await client.http.get(f"/api/kb/{kb_id}/standard-answers")).json()
    assert [(r["id"], r["status"]) for r in rows] == [(out["id"], "pending_review")]



async def test_submitted_standard_answer_is_marked_as_mcp_source(
        tmp_path, fake_embedder):
    """T14+T13 联动：MCP 提交的标问必须记 source=mcp，而不是与人工录入混为一谈。

    审核队列里人提的和 Agent 提的混在一起，但追溯责任方完全不同——若 MCP 提交
    被记成 manual，运营就无法判断某条标问是同事写的还是 Agent 自动提的。
    """
    from fastapi.testclient import TestClient

    from kbase.api.main import create_app
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    c0 = TestClient(app)
    kb = c0.post("/api/kb", json={"name": "标问库"}).json()["id"]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://kbase.test") as http:
        out = await submit_standard_answer_impl(
            KBaseClient(http), kb, "住房补贴怎么申领", "满两年可申领")
    assert out.get("id") and out.get("status") == "pending_review"

    rows = c0.get(f"/api/kb/{kb}/standard-answers").json()
    row = next(r for r in rows if r["id"] == out["id"])
    assert row["source"] == "mcp", f"MCP 提交被记成了 {row['source']}"

    # 对照：管理端手工录入仍是 manual（默认值不变，既有行为零变化）
    manual = c0.post(f"/api/kb/{kb}/standard-answers",
                     json={"question": "手工提的问题", "answer": "手工答案"})
    assert manual.status_code == 200, manual.text
    rows = c0.get(f"/api/kb/{kb}/standard-answers").json()
    assert next(r for r in rows if r["id"] == manual.json()["id"])["source"] == "manual"
