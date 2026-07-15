"""问答反馈闭环（M6-4）：点赞/点踩落库 + 差评清单/计数（喂运营看板）。

反馈对象是助手消息（answer）。一条消息至多一条反馈：重复提交覆盖旧值
（用户改主意点赞→踩是常见操作，不留双记录）。差评清单把"哪个问题答砸了"
直接暴露给运营——配合无答案清单（拒答）覆盖两类质量事故：答不上、答错了。
"""
import uuid

from kbase.models import Conversation, Message, MessageFeedback


def upsert_feedback(sf, message_id: str, rating: int,
                    note: str | None = None) -> dict:
    """覆盖式写入。调用方已完成消息存在性与归属校验。"""
    with sf() as s:
        row = (s.query(MessageFeedback)
               .filter(MessageFeedback.message_id == message_id).first())
        if row is None:
            row = MessageFeedback(id=str(uuid.uuid4()), message_id=message_id,
                                  conv_id="", rating=rating, note=note)
            msg = s.get(Message, message_id)
            row.conv_id = msg.conv_id if msg else ""
            s.add(row)
        else:
            row.rating = rating
            row.note = note
        s.commit()
        return {"message_id": message_id, "rating": rating, "note": note}


def feedback_stats(sf) -> dict:
    """总量计数：赞/踩，运营看板顶部指标用。"""
    with sf() as s:
        ups = s.query(MessageFeedback).filter(MessageFeedback.rating > 0).count()
        downs = s.query(MessageFeedback).filter(MessageFeedback.rating < 0).count()
    return {"up": ups, "down": downs}


def negative_list(sf, limit: int = 50) -> list[dict]:
    """差评清单（新→旧）：带回该轮的问题原文（同会话中该助手消息的前一条
    user 消息）与答案摘录，运营不用逐会话翻。"""
    with sf() as s:
        rows = (s.query(MessageFeedback, Message)
                .join(Message, Message.id == MessageFeedback.message_id)
                .filter(MessageFeedback.rating < 0)
                .order_by(MessageFeedback.created_at.desc())
                .limit(limit).all())
        items = []
        for fb, msg in rows:
            question = (s.query(Message)
                        .filter(Message.conv_id == msg.conv_id,
                                Message.seq < msg.seq,
                                Message.role == "user")
                        .order_by(Message.seq.desc()).first())
            conv = s.get(Conversation, msg.conv_id)
            items.append({
                "message_id": msg.id,
                "kb_id": conv.kb_id if conv else None,
                "question": question.content if question else None,
                "answer_excerpt": msg.content[:200],
                "note": fb.note,
                "created_at": fb.created_at.isoformat(),
            })
        return items
