"""飞书群机器人（对标 #2）：url_verification 握手（明文/加密）、签名校验、
消息事件→检索生成→卡片回复全流程（飞书网络层全打桩）、event_id 去重、
非文本消息静默忽略。"""
import base64
import hashlib
import json
import os

import pytest
from fastapi.testclient import TestClient

import kbase.feishu as feishu
import kbase.feishu_bot as feishu_bot
from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM


@pytest.fixture
def client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def _setup_bot(client, *, encrypt_key=None) -> str:
    """建库+传文档+配置机器人，返回 kb_id。"""
    kb = client.post("/api/kb", json={"name": "机器人库"}).json()["id"]
    r = client.post(f"/api/kb/{kb}/documents",
                    files=[("files", ("报销.md",
                                      "# 报销制度\n住宿上限每晚500元。".encode(),
                                      "text/markdown"))])
    assert r.status_code == 200, r.text
    body = {"verification_token": "vt-123", "kb_id": kb}
    if encrypt_key:
        body["encrypt_key"] = encrypt_key
    r = client.put("/api/settings/feishu-bot", json=body)
    assert r.status_code == 200, r.text
    return kb


def _encrypt(encrypt_key: str, payload: dict) -> str:
    """测试向量：与飞书同算法加密（AES-256-CBC + PKCS7，IV 前置）。"""
    from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                        modes)
    plain = json.dumps(payload).encode()
    pad = 16 - len(plain) % 16
    plain += bytes([pad]) * pad
    iv = os.urandom(16)
    key = hashlib.sha256(encrypt_key.encode()).digest()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + enc.update(plain) + enc.finalize()).decode()


def _message_event(event_id: str, text: str) -> dict:
    return {
        "header": {"token": "vt-123", "event_id": event_id,
                   "event_type": "im.message.receive_v1"},
        "event": {"message": {
            "message_id": f"om-{event_id}", "message_type": "text",
            "content": json.dumps({"text": f"@_user_1 {text}"}),
            "mentions": [{"key": "@_user_1"}]}},
    }


def test_url_verification_plain_and_token_check(client):
    _setup_bot(client)
    r = client.post("/api/feishu/events", json={
        "type": "url_verification", "token": "vt-123", "challenge": "abc"})
    assert r.status_code == 200 and r.json() == {"challenge": "abc"}
    # 错 token 拒绝
    assert client.post("/api/feishu/events", json={
        "type": "url_verification", "token": "wrong", "challenge": "x"}
    ).status_code == 403


def test_unconfigured_still_answers_handshake(client):
    # 未配置也要能完成握手（飞书验证回调地址在填 token 之前），只有真实业务事件才要求已配置
    r = client.post("/api/feishu/events",
                    json={"type": "url_verification", "challenge": "c-1"})
    assert r.status_code == 200 and r.json() == {"challenge": "c-1"}
    assert client.post("/api/feishu/events",
                       json={"header": {"event_type": "im.message.receive_v1"}}
                       ).status_code == 403


def test_encrypted_challenge_and_bad_signature(client):
    _setup_bot(client, encrypt_key="ek-secret")
    enc = _encrypt("ek-secret", {"type": "url_verification",
                                 "token": "vt-123", "challenge": "enc-ok"})
    r = client.post("/api/feishu/events", json={"encrypt": enc})
    assert r.status_code == 200 and r.json() == {"challenge": "enc-ok"}

    # 带签名头但签名伪造 → 403
    r = client.post("/api/feishu/events", json={"encrypt": enc},
                    headers={"X-Lark-Signature": "forged",
                             "X-Lark-Request-Timestamp": "1",
                             "X-Lark-Request-Nonce": "n"})
    assert r.status_code == 403


def test_message_event_full_flow_and_dedup(client, monkeypatch):
    _setup_bot(client)
    monkeypatch.setattr(feishu, "get_credentials",
                        lambda sf: ("cli_x", "sec_x"))
    monkeypatch.setattr(feishu, "_get_token", lambda a, b: "fake-token")
    replies: list = []
    monkeypatch.setattr(feishu_bot, "reply_card",
                        lambda token, mid, card: replies.append((mid, card)))
    feishu_bot._seen_events.clear()

    r = client.post("/api/feishu/events",
                    json=_message_event("ev-1", "住宿上限是多少"))
    assert r.status_code == 200
    # TestClient 同步执行 BackgroundTasks：回复已发出
    assert len(replies) == 1
    mid, card = replies[0]
    assert mid == "om-ev-1"
    card_text = json.dumps(card, ensure_ascii=False)
    assert "引用来源" in card_text and "报销" in card_text

    # 同一 event_id 重推：确认但不重复回答
    client.post("/api/feishu/events", json=_message_event("ev-1", "住宿上限是多少"))
    assert len(replies) == 1

    # 非文本消息：静默忽略
    ev = _message_event("ev-2", "x")
    ev["event"]["message"]["message_type"] = "image"
    client.post("/api/feishu/events", json=ev)
    assert len(replies) == 1
