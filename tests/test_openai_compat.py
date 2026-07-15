"""OpenAI 兼容 API（M6-5）：/v1/models 列可见库、/v1/chat/completions
流式与非流式、model 不存在 404 错误格式、按库名解析、ACL 收紧后不可见。"""
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


def _kb_with_doc(c, name="制度库"):
    kb = c.post("/api/kb", json={"name": name}).json()["id"]
    c.post(f"/api/kb/{kb}/documents",
           files=[("files", ("补贴.md", "# 补贴\n住房补贴入职满两年可申领。".encode("utf-8"),
                             "text/markdown"))])
    return kb


def test_v1_models_lists_kbs(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb = _kb_with_doc(c)
    r = c.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert kb in ids
    m = next(m for m in body["data"] if m["id"] == kb)
    assert m["object"] == "model" and m["display_name"] == "制度库"


def test_chat_completions_non_stream(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb = _kb_with_doc(c)
    r = c.post("/v1/chat/completions", json={
        "model": kb,
        "messages": [{"role": "user", "content": "住房补贴怎么申领？"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    choice = body["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["role"] == "assistant"
    assert "满两年" in choice["message"]["content"]     # FakeLLM 固定输出
    # KBase 扩展：引用溯源
    assert body["citations"] and body["citations"][0]["doc_name"] == "补贴.md"


def test_chat_completions_stream(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb = _kb_with_doc(c)
    chunks, done = [], False
    with c.stream("POST", "/v1/chat/completions", json={
            "model": kb, "stream": True,
            "messages": [{"role": "user", "content": "住房补贴怎么申领？"}]}) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                break
            chunks.append(_json.loads(data))
    assert done, "流必须以 data: [DONE] 结尾"
    assert all(ch["object"] == "chat.completion.chunk" for ch in chunks)
    # 拼接 delta.content = 完整回答；末块 finish_reason=stop 且带 citations
    text = "".join(ch["choices"][0]["delta"].get("content", "") for ch in chunks)
    assert "满两年" in text
    last = chunks[-1]
    assert last["choices"][0]["finish_reason"] == "stop"
    assert last["citations"]


def test_model_not_found_openai_error(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    r = c.post("/v1/chat/completions", json={
        "model": "nope", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "model_not_found"


def test_model_by_unique_name(tmp_path, fake_embedder):
    """model 也可填库名（唯一时）；重名则解析失败 404，不猜。"""
    c = _client(tmp_path, fake_embedder)
    _kb_with_doc(c, name="财务制度")
    r = c.post("/v1/chat/completions", json={
        "model": "财务制度",
        "messages": [{"role": "user", "content": "住房补贴怎么申领？"}]})
    assert r.status_code == 200
    assert "满两年" in r.json()["choices"][0]["message"]["content"]

    # 建同名第二库 → 名字不再唯一 → 404
    c.post("/api/kb", json={"name": "财务制度"})
    r2 = c.post("/v1/chat/completions", json={
        "model": "财务制度",
        "messages": [{"role": "user", "content": "hi"}]})
    assert r2.status_code == 404


def test_acl_restricts_v1(tmp_path, fake_embedder, monkeypatch):
    """auth=on：库授权收紧后，未授权用户在 /v1/models 看不到、chat 打 404。"""
    monkeypatch.setenv("KBASE_ADMIN_PASSWORD", "admin-pw")
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="on")
    admin = TestClient(app)
    admin.post("/api/auth/login", json={"username": "admin", "password": "admin-pw"})
    alice = admin.post("/api/users", json={"username": "alice", "role": "viewer",
                                           "password": "pw"}).json()
    bob_c = TestClient(app)
    admin.post("/api/users", json={"username": "bob", "role": "viewer",
                                   "password": "pw2"})
    bob_c.post("/api/auth/login", json={"username": "bob", "password": "pw2"})

    kb = admin.post("/api/kb", json={"name": "机密库"}).json()["id"]
    admin.put(f"/api/kb/{kb}/grants", json={"user_ids": [alice["id"]]})

    assert kb not in [m["id"] for m in bob_c.get("/v1/models").json()["data"]]
    r = bob_c.post("/v1/chat/completions", json={
        "model": kb, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"
