"""飞书群机器人（对标清单 #2，FastGPT 一键接入模式）：在飞书群里 @机器人
或单聊提问，KBase 检索生成后以卡片回复（答案 + 引用来源）。

复用既有飞书自建应用（同一 app 开启"机器人"能力），本模块只管三件事：
1. 事件安全层：url_verification 握手、AES-256-CBC 解密（加密模式）、
   签名校验（sha256(timestamp+nonce+encrypt_key+body)）、verification
   token 核对（明文模式）；
2. 消息事件解析：im.message.receive_v1 → 提取纯文本问题（剥 @ 提及）；
3. 回复：interactive 卡片（markdown 元素），走 /im/v1/messages/{id}/reply。

配置存 AppSetting KV（与凭据/发件箱同规矩）：verification_token 与
encrypt_key 只写不回显；绑定的知识库/回答模型在管理页维护——终端群成员
无任何配置项（与免登录分享同一"构建者配置、使用者消费"原则）。
需要飞书权限：im:message（收）+ im:message:send_as_bot（发），事件订阅
im.message.receive_v1。
"""
import base64
import hashlib
import json
import logging
from collections import OrderedDict

import httpx

from kbase.models import AppSetting

logger = logging.getLogger(__name__)

VERIFICATION_TOKEN_KEY = "feishu_bot_verification_token"
ENCRYPT_KEY_KEY = "feishu_bot_encrypt_key"
BOT_KB_KEY = "feishu_bot_kb_id"
BOT_PROVIDER_KEY = "feishu_bot_provider"

_KEYS = {
    "verification_token": VERIFICATION_TOKEN_KEY,
    "encrypt_key": ENCRYPT_KEY_KEY,
    "kb_id": BOT_KB_KEY,
    "provider": BOT_PROVIDER_KEY,
}


def get_settings(sf) -> dict:
    with sf() as s:
        out = {}
        for field, key in _KEYS.items():
            row = s.get(AppSetting, key)
            out[field] = row.value if row else None
    return out


def set_settings(sf, *, verification_token: str | None,
                 encrypt_key: str | None, kb_id: str,
                 provider: str | None) -> None:
    """token/encrypt_key 传 None=保留旧值（编辑表单不回显的惯例）；
    kb_id 必填（机器人没有绑定库就没有回答依据）。"""
    values: dict = {"kb_id": kb_id, "provider": provider or ""}
    if verification_token is not None:
        values["verification_token"] = verification_token
    if encrypt_key is not None:
        values["encrypt_key"] = encrypt_key
    with sf() as s:
        for field, value in values.items():
            key = _KEYS[field]
            row = s.get(AppSetting, key)
            if row is None:
                s.add(AppSetting(key=key, value=value))
            else:
                row.value = value
        s.commit()


def status(sf) -> dict:
    """管理页脱敏视图：密钥只回配置与否。"""
    cfg = get_settings(sf)
    return {"configured": bool(cfg["verification_token"] and cfg["kb_id"]),
            "has_verification_token": bool(cfg["verification_token"]),
            "has_encrypt_key": bool(cfg["encrypt_key"]),
            "kb_id": cfg["kb_id"] or None,
            "provider": cfg["provider"] or None}


# ---- 事件安全层 ----

def decrypt_event(encrypt_key: str, encrypt_b64: str) -> dict:
    """飞书事件加密模式：AES-256-CBC，key=sha256(encrypt_key)，密文前
    16 字节为 IV，PKCS7 填充。解不开抛异常由调用方转 400。"""
    from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                        modes)
    data = base64.b64decode(encrypt_b64)
    key = hashlib.sha256(encrypt_key.encode()).digest()
    cipher = Cipher(algorithms.AES(key), modes.CBC(data[:16]))
    dec = cipher.decryptor()
    padded = dec.update(data[16:]) + dec.finalize()
    plain = padded[:-padded[-1]]                     # PKCS7
    return json.loads(plain.decode("utf-8"))


def verify_signature(encrypt_key: str, timestamp: str, nonce: str,
                     body: bytes, signature: str) -> bool:
    """加密模式请求头签名：sha256(timestamp + nonce + encrypt_key + body)。"""
    digest = hashlib.sha256(
        timestamp.encode() + nonce.encode() + encrypt_key.encode() + body
    ).hexdigest()
    return digest == signature


# ---- 消息解析 ----

def extract_question(event: dict) -> tuple[str, str] | None:
    """im.message.receive_v1 → (问题纯文本, message_id)。
    只收文本消息；剥掉 @机器人 的提及占位（@_user_1 形态）。
    解析不出（表情/图片/富文本等）返回 None，静默忽略不回错误——群里
    并非每条消息都是提问。"""
    message = (event.get("event") or {}).get("message") or {}
    if message.get("message_type") != "text":
        return None
    try:
        text = json.loads(message.get("content") or "{}").get("text", "")
    except (ValueError, TypeError):
        return None
    for mention in message.get("mentions") or []:
        key = mention.get("key")
        if key:
            text = text.replace(key, "")
    text = text.strip()
    message_id = message.get("message_id")
    if not text or not message_id:
        return None
    return text, message_id


# ---- 回复（卡片） ----

def build_answer_card(answer: str, citations: list[dict]) -> dict:
    """interactive 卡片：答案 markdown + 引用来源列表（去重取前 3 个文档）。
    正文里的 [n] 角标在卡片里没有可点目标，保留原样作为出处编号提示。"""
    seen: list[str] = []
    for c in citations:
        name = c.get("doc_name") or ""
        heading = (c.get("heading_path") or "").split(" > ")
        label = name + (f"（{heading[-1]}）" if len(heading) > 1 else "")
        if label and label not in seen:
            seen.append(label)
    refs = "\n".join(f"{i + 1}. {label}" for i, label in enumerate(seen[:3]))
    elements: list[dict] = [{"tag": "markdown", "content": answer}]
    if refs:
        elements += [{"tag": "hr"},
                     {"tag": "markdown", "content": f"**📄 引用来源**\n{refs}"}]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "KBase 知识库回答"},
                   "template": "purple"},
        "elements": elements,
    }


def reply_card(token: str, message_id: str, card: dict) -> None:
    """回复到原消息（群聊/单聊同一姿势）。失败抛异常由调用方记日志。"""
    from kbase.feishu import FEISHU_BASE
    resp = httpx.post(
        f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply",
        headers={"Authorization": f"Bearer {token}"},
        json={"msg_type": "interactive",
              "content": json.dumps(card, ensure_ascii=False)},
        timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书回复失败: {data.get('msg')} (code={data.get('code')})")


# ---- 事件去重（飞书超时未答会重推） ----

_seen_events: OrderedDict[str, None] = OrderedDict()
_SEEN_MAX = 1024


def is_duplicate_event(event_id: str) -> bool:
    """进程内 LRU 去重：重启丢失可接受（飞书重推窗口很短）。"""
    if not event_id:
        return False
    if event_id in _seen_events:
        return True
    _seen_events[event_id] = None
    while len(_seen_events) > _SEEN_MAX:
        _seen_events.popitem(last=False)
    return False
