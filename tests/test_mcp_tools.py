import httpx
import pytest

from kbase_mcp.server import KBaseClient, ask_knowledge_base_impl, \
    list_knowledge_bases_impl, search_knowledge_impl
from tests.test_api import CFG, MD, FakeLLM


@pytest.fixture
def kbase_app(tmp_path, fake_embedder):
    from kbase.api.main import create_app
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    return create_app(config_path=cfg, embedder=fake_embedder,
                      llms={"fake": FakeLLM()}, reranker=False)


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
                     llms={"fake": NewlineFakeLLM()}, reranker=False)

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
