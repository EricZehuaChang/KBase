"""会话领域逻辑：CRUD 与多轮上下文组装。HTTP 编排在 api/main.py。"""
import json
import uuid
from datetime import datetime, timedelta

from kbase.models import Conversation, Message

HISTORY_ROUNDS = 3


def create_conversation(sf, kb_id: str) -> dict:
    conv = Conversation(id=str(uuid.uuid4()), kb_id=kb_id)
    with sf() as s:
        s.add(conv)
        s.commit()
    return {"id": conv.id, "kb_id": conv.kb_id, "title": conv.title}


def list_conversations(sf, kb_id: str | None = None) -> list[dict]:
    with sf() as s:
        q = s.query(Conversation).order_by(Conversation.updated_at.desc())
        if kb_id:
            q = q.filter_by(kb_id=kb_id)
        return [{"id": c.id, "kb_id": c.kb_id, "title": c.title,
                 "updated_at": c.updated_at.isoformat()} for c in q.all()]


def list_messages(sf, conv_id: str) -> list[dict]:
    with sf() as s:
        msgs = (s.query(Message).filter_by(conv_id=conv_id)
                .order_by(Message.created_at, Message.id).all())
        return [{"id": m.id, "role": m.role, "content": m.content,
                 "citations": m.citations, "provider": m.provider} for m in msgs]


def build_history(sf, conv_id: str, rounds: int = HISTORY_ROUNDS) -> list[dict]:
    with sf() as s:
        msgs = (s.query(Message).filter_by(conv_id=conv_id)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(rounds * 2).all())
    return [{"role": m.role, "content": m.content} for m in reversed(msgs)]


def append_round(sf, conv_id: str, question: str, answer: str,
                 citations: list[dict], provider: str) -> None:
    with sf() as s:
        conv = s.get(Conversation, conv_id)
        if conv is None:
            return
        if not s.query(Message).filter_by(conv_id=conv_id).first():
            conv.title = question[:20]
        # 两条消息的 created_at 显式错开：同一函数内连续两次 datetime.utcnow()
        # 常落在同一微秒，若时间戳相同则排序只能靠 Message.id（UUID，随机序），
        # user/assistant 顺序会不稳定——用 microseconds=1 的间隔保证先后可排序。
        now = datetime.utcnow()
        s.add(Message(id=str(uuid.uuid4()), conv_id=conv_id, role="user",
                      content=question, created_at=now))
        s.add(Message(id=str(uuid.uuid4()), conv_id=conv_id, role="assistant",
                      content=answer, provider=provider,
                      citations=json.dumps(citations, ensure_ascii=False),
                      created_at=now + timedelta(microseconds=1)))
        conv.updated_at = now
        s.commit()
