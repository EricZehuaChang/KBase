"""会话领域逻辑：CRUD 与多轮上下文组装。HTTP 编排在 api/main.py。"""
import json
import re
import uuid
from datetime import datetime

from sqlalchemy import func

from kbase.models import Conversation, Message

HISTORY_ROUNDS = 3


def create_conversation(sf, kb_id: str) -> dict:
    conv = Conversation(id=str(uuid.uuid4()), kb_id=kb_id)
    with sf() as s:
        s.add(conv)
        s.commit()
    return {"id": conv.id, "kb_id": conv.kb_id, "title": conv.title}


def list_conversations(sf, kb_id: str | None = None, *,
                       limit: int = 30, offset: int = 0) -> dict:
    """按 updated_at desc 分页；返回 {items, total}——total 为过滤后（按 kb_id）
    的总数，供前端判断是否还有更多可加载。"""
    with sf() as s:
        q = s.query(Conversation)
        if kb_id:
            q = q.filter_by(kb_id=kb_id)
        total = q.count()
        rows = (q.order_by(Conversation.updated_at.desc())
                .limit(limit).offset(offset).all())
        items = [{"id": c.id, "kb_id": c.kb_id, "title": c.title,
                 "updated_at": c.updated_at.isoformat()} for c in rows]
        return {"items": items, "total": total}


def list_messages(sf, conv_id: str) -> list[dict]:
    with sf() as s:
        msgs = (s.query(Message).filter_by(conv_id=conv_id)
                .order_by(Message.seq).all())
        return [{"id": m.id, "role": m.role, "content": m.content,
                 "citations": m.citations, "provider": m.provider} for m in msgs]


def build_history(sf, conv_id: str, rounds: int = HISTORY_ROUNDS) -> list[dict]:
    with sf() as s:
        msgs = (s.query(Message).filter_by(conv_id=conv_id)
                .order_by(Message.seq.desc())
                .limit(rounds * 2).all())
    out = []
    for m in reversed(msgs):
        content = m.content
        if m.role == "assistant":
            # 历史中的引用编号与当前轮 sources 编号无关，剥离以免模型串号（存储原文不动）
            content = re.sub(r"\[\d+\]", "", content)
        out.append({"role": m.role, "content": content})
    return out


def append_round(sf, conv_id: str, question: str, answer: str,
                 citations: list[dict], provider: str) -> None:
    with sf() as s:
        conv = s.get(Conversation, conv_id)
        if conv is None:
            return
        if not s.query(Message).filter_by(conv_id=conv_id).first():
            conv.title = question[:20]
        # 显式序列列根治排序：时间戳在 Windows 上刻度粗（0.5~8ms），连续轮次
        # 可落在同一刻度导致排序退化为 UUID 随机序。SQLite 单写者串行化，
        # 同一事务内 max+1 无竞态。
        base = (s.query(func.max(Message.seq))
                .filter_by(conv_id=conv_id).scalar() or 0)
        now = datetime.utcnow()
        s.add(Message(id=str(uuid.uuid4()), conv_id=conv_id, role="user",
                      content=question, seq=base + 1, created_at=now))
        s.add(Message(id=str(uuid.uuid4()), conv_id=conv_id, role="assistant",
                      content=answer, provider=provider,
                      citations=json.dumps(citations, ensure_ascii=False),
                      seq=base + 2, created_at=now))
        conv.updated_at = now
        s.commit()
