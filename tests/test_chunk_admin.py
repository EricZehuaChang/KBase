"""Chunk 运营管理（M6-1）：列表分页/过滤、启停=索引成员管理、编辑重嵌入。"""
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, MD, FakeLLM


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def _kb_with_doc(c):
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    doc = c.get(f"/api/kb/{kb_id}/documents").json()[0]
    return kb_id, doc["id"]


def test_list_chunks_pagination_and_filter(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    _kb, doc_id = _kb_with_doc(c)
    got = c.get(f"/api/documents/{doc_id}/chunks").json()
    assert got["total"] >= 2                       # 至少 1 父 + 1 叶
    assert all({"id", "text", "is_leaf", "enabled", "chars"} <= set(i)
               for i in got["items"])
    assert got["items"][0]["is_leaf"] is True      # 叶子在前
    assert all(i["enabled"] is True for i in got["items"])
    # 文本过滤
    hit = c.get(f"/api/documents/{doc_id}/chunks", params={"q": "住房补贴"}).json()
    assert hit["total"] >= 1
    miss = c.get(f"/api/documents/{doc_id}/chunks", params={"q": "不存在XYZ"}).json()
    assert miss["total"] == 0
    assert c.get("/api/documents/nope/chunks").status_code == 404


def test_disable_chunk_removes_from_retrieval(tmp_path, fake_embedder):
    """停用=索引成员摘除：停用命中叶子后，同一查询不再召回该块；恢复后可再召回。"""
    c = _client(tmp_path, fake_embedder)
    kb_id, doc_id = _kb_with_doc(c)
    blocks = c.post(f"/api/kb/{kb_id}/search",
                    json={"query": "连续工作满两年可申领住房补贴。", "top_k": 5}).json()["blocks"]
    assert blocks, "基线应能召回"

    leaves = c.get(f"/api/documents/{doc_id}/chunks").json()["items"]
    leaf_ids = [i["id"] for i in leaves if i["is_leaf"]]
    for cid in leaf_ids:                            # 停用全部叶子
        r = c.put(f"/api/chunks/{cid}", json={"enabled": False})
        assert r.status_code == 200 and r.json()["enabled"] is False

    blocks = c.post(f"/api/kb/{kb_id}/search",
                    json={"query": "连续工作满两年可申领住房补贴。", "top_k": 5}).json()["blocks"]
    assert blocks == []                             # 全部停用后不可召回

    for cid in leaf_ids:                            # 恢复
        c.put(f"/api/chunks/{cid}", json={"enabled": True})
    blocks = c.post(f"/api/kb/{kb_id}/search",
                    json={"query": "连续工作满两年可申领住房补贴。", "top_k": 5}).json()["blocks"]
    assert blocks, "恢复后应可再召回"


def test_edit_leaf_reembeds_and_reindexes(tmp_path, fake_embedder):
    """编辑叶子文本后：新词可被关键词路召回，snippet 返回新文本，且 FTS
    无重复行（编辑两次仍只召回一次）。"""
    c = _client(tmp_path, fake_embedder)
    kb_id, doc_id = _kb_with_doc(c)
    leaf = next(i for i in c.get(f"/api/documents/{doc_id}/chunks").json()["items"]
                if i["is_leaf"])
    r = c.put(f"/api/chunks/{leaf['id']}",
              json={"text": "特批通道：工龄一年即可申领特殊住房补贴金卡。"})
    assert r.status_code == 200
    assert "特批通道" in r.json()["text"]

    blocks = c.post(f"/api/kb/{kb_id}/search",
                    json={"query": "特殊住房补贴金卡", "top_k": 5}).json()["blocks"]
    assert blocks and "特批通道" in blocks[0]["snippet"]
    # 再编辑一次，确认重索引是替换不是追加（不产生重复候选）
    c.put(f"/api/chunks/{leaf['id']}",
          json={"text": "特批通道：工龄一年即可申领特殊住房补贴金卡（修订）。"})
    blocks = c.post(f"/api/kb/{kb_id}/search",
                    json={"query": "特殊住房补贴金卡", "top_k": 5}).json()["blocks"]
    assert len([b for b in blocks if "特批通道" in b["snippet"]]) == 1


def test_update_chunk_validation(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    _kb, doc_id = _kb_with_doc(c)
    leaf = next(i for i in c.get(f"/api/documents/{doc_id}/chunks").json()["items"]
                if i["is_leaf"])
    assert c.put(f"/api/chunks/{leaf['id']}", json={}).status_code == 422
    assert c.put(f"/api/chunks/{leaf['id']}",
                 json={"text": "   "}).status_code == 422
    assert c.put("/api/chunks/nope", json={"enabled": False}).status_code == 404
