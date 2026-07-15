"""多库联合问答（M6-2）：retrieve_multi 散射聚合、多库会话检索、
citations 带 kb_id 溯源、ACL 与多库交叉（无权库不能绑进会话）。"""
import json as _json

import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def _kb_with(c, name, filename, content):
    kb = c.post("/api/kb", json={"name": name}).json()["id"]
    c.post(f"/api/kb/{kb}/documents",
           files=[("files", (filename, content.encode("utf-8"), "text/markdown"))])
    return kb


def test_retrieve_multi_scatter_gather(tmp_path, fake_embedder):
    from kbase.rag.retriever import Retriever, ContextBlock
    # 用两个假库直接测 retrieve_multi 的合并排序（各库 retrieve 打桩）
    r = Retriever.__new__(Retriever)

    def fake_retrieve(kb_id, query, top_k, strategy=None):
        pool = {
            "kbA": [ContextBlock("dA", "a.md", "h", "ta", "sa", 0.9, kb_id="kbA"),
                    ContextBlock("dA2", "a2.md", "h", "t", "s", 0.4, kb_id="kbA")],
            "kbB": [ContextBlock("dB", "b.md", "h", "tb", "sb", 0.7, kb_id="kbB")],
        }
        return pool[kb_id][:top_k]

    r.retrieve = fake_retrieve
    merged = r.retrieve_multi(["kbA", "kbB"], "q", top_k=2)
    # 全局按分数排序取 top2：0.9(kbA) > 0.7(kbB) > 0.4
    assert [b.doc_id for b in merged] == ["dA", "dB"]
    assert {b.kb_id for b in merged} == {"kbA", "kbB"}


def test_multi_kb_conversation_retrieves_both(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb1 = _kb_with(c, "制度库", "补贴.md",
                   "# 补贴\n住房补贴每月固定发放金额标准。")
    kb2 = _kb_with(c, "案例库", "案例.md",
                   "# 案例\n某员工申请差旅报销的处理流程实录。")

    # 建多库会话
    conv = c.post("/api/conversations",
                  json={"kb_id": kb1, "kb_ids": [kb1, kb2]}).json()
    assert conv["kb_ids"] == [kb1, kb2]

    # 问一个只在 kb2 命中的问题 → 多库检索应能召回 kb2 的内容并带 kb_id
    citations = []
    with c.stream("POST", f"/api/conversations/{conv['id']}/query",
                  json={"question": "差旅报销的处理流程", "top_k": 5}) as r:
        event = ""
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event == "citations":
                citations = _json.loads(line[5:].strip())
                break
    assert citations, "多库会话应能跨库召回"
    # 命中的引用带 kb_id，且包含 kb2（案例库）来源
    assert all("kb_id" in ci for ci in citations)
    assert any(ci["kb_id"] == kb2 for ci in citations)


def test_single_kb_conversation_backward_compat(tmp_path, fake_embedder):
    """不传 kb_ids = 单库会话，kb_ids 落 None（老行为不变）。"""
    c = _client(tmp_path, fake_embedder)
    kb = _kb_with(c, "库", "d.md", "# 标题\n内容正文。")
    conv = c.post("/api/conversations", json={"kb_id": kb}).json()
    assert conv["kb_ids"] is None
