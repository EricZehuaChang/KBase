"""换绑向量模型（vault 待办收尾）：换绑触发全库重建、停用块不随重建复活
（顺手修复 reindex_kb 的 M6-1 缺口）、同模型 409、未知 422、密钥缺失 503。"""
import pytest
from fastapi.testclient import TestClient

import kbase.plugins.embedders.factory as factory_mod
from kbase.api.main import create_app
from tests.conftest import FakeEmbedder
from tests.test_api import CFG, FakeLLM

CFG_EMB = CFG + """
embedders:
  - id: cloud-embed
    plugin: openai-embed
    base_url: https://embed.example/v1
    api_key_env: TEST_EMBED_KEY_ABSENT
    model: text-embedding-test
"""


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG_EMB.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def test_rebind_rebuilds_and_keeps_disabled_chunks(tmp_path, fake_embedder,
                                                   monkeypatch):
    # cloud-embed 打桩成 FakeEmbedder（同向量语义，换绑后检索仍可比对命中）
    monkeypatch.setattr(factory_mod, "build_option_embedder",
                        lambda opt, api_key=None: FakeEmbedder())
    c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    doc = c.post(f"/api/kb/{kb}/documents", files=[
        ("files", ("制度.md",
                   "# 制度\n## 补贴\n住房补贴满两年可申领。\n## 考勤\n迟到三次记警告。"
                   .encode("utf-8"), "text/markdown"))])
    assert doc.status_code == 200
    doc_id = c.get(f"/api/kb/{kb}/documents").json()[0]["id"]

    # 停用"考勤"叶子块（M6-1）
    chunks = c.get(f"/api/documents/{doc_id}/chunks").json()["items"]
    kaoqin = next(ch for ch in chunks if ch["is_leaf"] and "迟到" in ch["text"])
    c.put(f"/api/chunks/{kaoqin['id']}", json={"enabled": False})

    # 换绑到 cloud-embed → 后台重建（TestClient 的 bg 同步执行完才返回）
    r = c.post(f"/api/kb/{kb}/rebind-embedder", json={"embedder": "cloud-embed"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "from": "default", "to": "cloud-embed"}

    # 绑定已落 KB.config
    kb_row = next(k for k in c.get("/api/kb").json() if k["id"] == kb)
    assert kb_row["config"]["embedder"] == "cloud-embed"

    # 重建后：启用块可检索命中；停用块不复活
    hits = c.post(f"/api/kb/{kb}/search",
                  json={"query": "住房补贴申领", "top_k": 5}).json()["blocks"]
    assert hits, "换绑重建后启用块必须仍可检索"
    kaoqin_hits = c.post(f"/api/kb/{kb}/search",
                         json={"query": "迟到三次警告", "top_k": 5}).json()["blocks"]
    assert not any("迟到三次" in b["text"] for b in kaoqin_hits), \
        "停用块不得随重建复活"

    # 同模型重复换绑 → 409
    assert c.post(f"/api/kb/{kb}/rebind-embedder",
                  json={"embedder": "cloud-embed"}).status_code == 409


def test_rebind_guards(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    # 未知模型 422；库不存在 404
    assert c.post(f"/api/kb/{kb}/rebind-embedder",
                  json={"embedder": "nope"}).status_code == 422
    assert c.post("/api/kb/nope/rebind-embedder",
                  json={"embedder": "cloud-embed"}).status_code == 404
    # cloud-embed 未配密钥（env 缺失且无 DB 覆盖）→ 503 当场失败，
    # 绑定不动（防"绑定已换但向量还是旧空间"的坏状态）
    r = c.post(f"/api/kb/{kb}/rebind-embedder", json={"embedder": "cloud-embed"})
    assert r.status_code == 503
    kb_row = next(k for k in c.get("/api/kb").json() if k["id"] == kb)
    assert kb_row["config"] is None
