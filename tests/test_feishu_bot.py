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


def _message_event(event_id: str, text: str, *, open_id: str | None = None) -> dict:
    """一条 im.message.receive_v1 事件。

    T19：open_id 是提问者的外部身份（sender.sender_id.open_id），渠道身份映射
    按它查绑定；不传时事件里没有 sender——未映射默认策略那条路。
    """
    event: dict = {
        "header": {"token": "vt-123", "event_id": event_id,
                   "event_type": "im.message.receive_v1"},
        "event": {"message": {
            "message_id": f"om-{event_id}", "message_type": "text",
            "content": json.dumps({"text": f"@_user_1 {text}"}),
            "mentions": [{"key": "@_user_1"}]}},
    }
    if open_id:
        event["event"]["sender"] = {"sender_id": {"open_id": open_id}}
    return event


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


# ---- T19 渠道身份映射：同一句提问，不同的人拿到不同结果 ----

def _mute_reply(monkeypatch) -> list:
    """把飞书网络层打桩，返回收集 (message_id, card) 的列表。"""
    monkeypatch.setattr(feishu, "get_credentials", lambda sf: ("cli_x", "sec_x"))
    monkeypatch.setattr(feishu, "_get_token", lambda a, b: "fake-token")
    replies: list = []
    monkeypatch.setattr(feishu_bot, "reply_card",
                        lambda token, mid, card: replies.append((mid, card)))
    feishu_bot._seen_events.clear()
    return replies


def _card_text(replies: list, idx: int = -1) -> str:
    return json.dumps(replies[idx][1], ensure_ascii=False)


def _outcomes(client):
    """飞书渠道的归因行（新→旧）：T12 表就是"谁问了什么、结果如何"的台账。"""
    from kbase.models import QaOutcome
    with client.app.state.svc.sf() as s:
        rows = (s.query(QaOutcome).filter(QaOutcome.channel == "feishu")
                .order_by(QaOutcome.ts.desc(), QaOutcome.id.desc()).all())
        return [{"actor": r.actor, "bucket": r.bucket,
                 "question": r.question, "retrieved_count": r.retrieved_count} for r in rows]


def _new_user(client, username: str, role: str = "viewer") -> str:
    r = client.post("/api/users", json={"username": username, "role": role,
                                        "password": "pw-123456"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_identity_mapping_decides_who_gets_an_answer(client, monkeypatch):
    """同一个库、同一句提问：映射到被授权用户的查得到，映射到无权用户的被拒答。

    这是渠道身份映射存在的全部理由——没有映射时机器人只能用一个笼统身份查库，
    "谁能问什么"无从表达，渠道就成了权限旁路。这里同时钉住：
    - 无权者的卡片里**没有引用**（不是"少给几条引用"），与网页端静默拒答同义；
    - 归因行的 actor 分别是两个真实用户名（不是 feishu-bot），bucket 一个
      answered、一个 scope_denied（安全事件），运营在归因面板上能看出来。
    """
    kb = _setup_bot(client)
    replies = _mute_reply(monkeypatch)
    authorized = _new_user(client, "feishu-ok")
    stranger = _new_user(client, "feishu-no")
    # 收紧该库：授权集合里只有 authorized —— stranger 对它无权（公开库变受限库）
    r = client.put(f"/api/kb/{kb}/grants", json={"user_ids": [authorized]})
    assert r.status_code == 200, r.text

    # 绑定两个外部账号（open_id 是渠道内不透明字符串）
    for open_id, uid in (("ou-ok", authorized), ("ou-no", stranger)):
        rb = client.post("/api/channels/identities",
                         json={"channel": "feishu", "external_user_id": open_id,
                               "user_id": uid})
        assert rb.status_code == 200, rb.text
        assert rb.json()["channel"] == "feishu"

    client.post("/api/feishu/events",
                json=_message_event("ev-ok", "住宿上限是多少", open_id="ou-ok"))
    client.post("/api/feishu/events",
                json=_message_event("ev-no", "住宿上限是多少", open_id="ou-no"))
    assert len(replies) == 2

    # 被授权者：拿到答案 + 引用来源
    ok_card = _card_text(replies, 0)
    assert "引用来源" in ok_card and "报销" in ok_card
    # 无权者：拒答文案，**没有**引用来源（静默拒答：不提示"你无权"）
    no_card = _card_text(replies, 1)
    assert "引用来源" not in no_card
    assert "未找到依据" in no_card

    rows = _outcomes(client)
    assert len(rows) == 2
    assert rows[0]["actor"] == "feishu-no" and rows[0]["bucket"] == "scope_denied"
    assert rows[1]["actor"] == "feishu-ok" and rows[1]["bucket"] == "answered"
    assert rows[1]["retrieved_count"] > 0        # 有权的那次真的检索了
    assert rows[0]["retrieved_count"] == 0       # 越权那次压根没触达检索器
    # 两次问的是同一句话——结果不同只可能来自身份
    assert rows[0]["question"] == rows[1]["question"] == "住宿上限是多少"


def test_unmapped_user_follows_default_policy(client, monkeypatch):
    """未映射（或没带 sender 的事件）走默认策略：**不是"全放行"**。

    默认策略 = 匿名 viewer（user_id=None）：公开库照常能问，收紧过的库问不到
    （与登录用户的可见性规则同一套 kb_acl 判定）。解绑后同样立刻回落默认策略。
    """
    kb = _setup_bot(client)
    replies = _mute_reply(monkeypatch)

    # 公开库（无 grant 行）：未映射的人能问
    client.post("/api/feishu/events",
                json=_message_event("ev-anon-1", "住宿上限是多少",
                                    open_id="ou-nobody"))
    assert "引用来源" in _card_text(replies)
    assert _outcomes(client)[0]["actor"] == "feishu:ou-nobody"

    # 事件里没有 sender：同样按未映射处理（actor 名退化成 feishu:unknown）
    client.post("/api/feishu/events",
                json=_message_event("ev-anon-2", "住宿上限是多少"))
    assert _outcomes(client)[0]["actor"] == "feishu:unknown"

    # 把库收紧到某个用户：未映射的人立刻问不到（默认策略随库的 ACL 走）
    owner = _new_user(client, "feishu-owner")
    assert client.put(f"/api/kb/{kb}/grants",
                      json={"user_ids": [owner]}).status_code == 200
    client.post("/api/feishu/events",
                json=_message_event("ev-anon-3", "住宿上限是多少",
                                    open_id="ou-nobody"))
    assert "引用来源" not in _card_text(replies)
    assert _outcomes(client)[0]["bucket"] == "scope_denied"

    # 绑定后再问：有权了（默认策略不是"永久拒绝"）
    rb = client.post("/api/channels/identities",
                     json={"channel": "feishu", "external_user_id": "ou-nobody",
                           "user_id": owner})
    assert rb.status_code == 200, rb.text
    identity_id = rb.json()["id"]
    client.post("/api/feishu/events",
                json=_message_event("ev-anon-4", "住宿上限是多少",
                                    open_id="ou-nobody"))
    assert "引用来源" in _card_text(replies)
    assert _outcomes(client)[0]["actor"] == "feishu-owner"

    # 解绑：立刻回落默认策略（resolve_actor 不缓存，管理端改完就该生效）
    assert client.delete(
        f"/api/channels/identities/{identity_id}").status_code == 200
    client.post("/api/feishu/events",
                json=_message_event("ev-anon-5", "住宿上限是多少",
                                    open_id="ou-nobody"))
    assert "引用来源" not in _card_text(replies)
    assert _outcomes(client)[0]["actor"] == "feishu:ou-nobody"


def test_disabled_or_deleted_user_is_unmapped(client, monkeypatch):
    """绑定的用户被停用/删除 → 等同未映射（默认策略）。

    不静默沿用旧角色：停用账号还能通过渠道继续问答，等于停用没生效。
    """
    kb = _setup_bot(client)
    replies = _mute_reply(monkeypatch)
    # 用 viewer 而不是 admin：admin 会被 ACL 豁免（"停用后不再豁免"这条就测不到），
    # 且 auth="off" 下系统里只有一个启用 admin，停用它会撞上"不能禁用最后一个
    # 管理员"的不变量。
    uid = _new_user(client, "feishu-temp", role="viewer")
    assert client.put(f"/api/kb/{kb}/grants",
                      json={"user_ids": [uid]}).status_code == 200
    rb = client.post("/api/channels/identities",
                     json={"channel": "feishu", "external_user_id": "ou-temp",
                           "user_id": uid})
    assert rb.status_code == 200, rb.text

    # 对照：停用前问得到（授权集合里就是他）
    client.post("/api/feishu/events",
                json=_message_event("ev-temp-0", "住宿上限是多少",
                                    open_id="ou-temp"))
    assert "引用来源" in _card_text(replies)
    assert _outcomes(client)[0]["actor"] == "feishu-temp"

    # 停用该用户：绑定还在，但不再沿用他的角色 → 等同未映射
    assert client.put(f"/api/users/{uid}",
                      json={"disabled": True}).status_code == 200
    client.post("/api/feishu/events",
                json=_message_event("ev-temp", "住宿上限是多少",
                                    open_id="ou-temp"))
    assert "引用来源" not in _card_text(replies)
    assert _outcomes(client)[0]["actor"] == "feishu:ou-temp"
    assert _outcomes(client)[0]["bucket"] == "scope_denied"


def test_channel_identity_admin_api(client):
    """绑定接口：清单可见（带用户名）、重复绑定是改绑、渠道/用户校验、解绑 404。"""
    kb = _setup_bot(client)
    assert kb
    uid_a = _new_user(client, "chan-a")
    uid_b = _new_user(client, "chan-b")

    assert client.get("/api/channels").json()["items"] == [
        {"channel": "feishu", "label": "飞书"}]
    assert client.get("/api/channels/identities").json()["items"] == []

    first = client.post("/api/channels/identities",
                        json={"channel": "feishu", "external_user_id": "ou-1",
                              "user_id": uid_a}).json()
    items = client.get("/api/channels/identities").json()["items"]
    assert len(items) == 1
    assert items[0]["username"] == "chan-a" and items[0]["role"] == "viewer"

    # 同一外部账号再绑 = 改绑（覆盖那一行，不留双份）
    again = client.post("/api/channels/identities",
                        json={"channel": "feishu", "external_user_id": "ou-1",
                              "user_id": uid_b}).json()
    assert again["id"] == first["id"]
    assert client.get("/api/channels/identities").json()["items"][0]["username"] \
        == "chan-b"

    # 未在册渠道 / 不存在的用户 / 空外部 id 一律 422（不写脏库）
    assert client.post("/api/channels/identities",
                       json={"channel": "dingtalk", "external_user_id": "x",
                             "user_id": uid_a}).status_code == 422
    assert client.post("/api/channels/identities",
                       json={"channel": "feishu", "external_user_id": "ou-2",
                             "user_id": "no-such-user"}).status_code == 422
    assert client.post("/api/channels/identities",
                       json={"channel": "feishu", "external_user_id": "  ",
                             "user_id": uid_a}).status_code == 422
    # 按渠道过滤
    assert len(client.get("/api/channels/identities?channel=feishu")
               .json()["items"]) == 1
    # 解绑：命中 200，再删同一条 404（不泄漏存在性）
    assert client.delete(
        f"/api/channels/identities/{first['id']}").status_code == 200
    assert client.get("/api/channels/identities").json()["items"] == []
    assert client.delete(
        f"/api/channels/identities/{first['id']}").status_code == 404


def test_orphan_binding_is_purged_on_list(client):
    """绑定的用户被删除后，孤儿绑定行在清单读取时自愈清理。

    用户的删除流程不认这张表（只级联清理会话/消息/反馈/授权行），所以这里直接
    删用户行模拟那个窄窗口：功能上无害（未映射兜底），但列表里不该留下"用户
    缺失"的脏行。停用的用户**不清理**——可恢复状态，且要如实显示。
    """
    _setup_bot(client)
    uid = _new_user(client, "chan-gone")
    assert client.post("/api/channels/identities",
                       json={"channel": "feishu", "external_user_id": "ou-gone",
                             "user_id": uid}).status_code == 200
    assert len(client.get("/api/channels/identities").json()["items"]) == 1

    from kbase.models import User
    with client.app.state.svc.sf() as s:
        s.delete(s.get(User, uid))
        s.commit()
    # 清单读取顺带回收：孤儿行不再出现
    assert client.get("/api/channels/identities").json()["items"] == []
    from kbase.models import ChannelIdentity
    with client.app.state.svc.sf() as s:
        assert s.query(ChannelIdentity).count() == 0
