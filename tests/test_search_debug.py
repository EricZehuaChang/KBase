from tests.test_settings_api import _client
from tests.test_api import MD


def _kb_with_doc(c):
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    return kb_id


def test_search_plain(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = _kb_with_doc(c)
    q = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    r = c.post(f"/api/kb/{kb_id}/search", json={"query": q, "top_k": 3}).json()
    assert r["blocks"] and "连续工作满两年" in r["blocks"][0]["text"]
    assert "trace" not in r


def test_search_debug_trace(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb_id = _kb_with_doc(c)
    r = c.post(f"/api/kb/{kb_id}/search",
               json={"query": "住房补贴", "top_k": 3, "debug": True}).json()
    assert set(r["trace"]) >= {"dense", "keyword", "fused"}
