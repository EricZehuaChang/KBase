"""会话领域逻辑：CRUD 与多轮上下文组装。HTTP 编排在 api/main.py。"""
import json
import re
import uuid
from datetime import datetime

from sqlalchemy import func, or_

from kbase.models import Conversation, Message

HISTORY_ROUNDS = 3


def _visible_filter(user_id: str | None):
    """会话归属过滤条件：本人会话 OR 历史遗留会话（user_id IS NULL）。

    取舍（M5-1 F2 的产品决策，记在这里而不是 API 层，因为所有归属相关的
    查询——list/get/rename/delete——都要复用同一条规则，散在各处容易漏改
    一处）：鉴权改造前的会话没有归属人，迁移时不倒推补全（没有可靠依据判断
    "这条老会话该归谁"），而是把 NULL 会话对所有登录用户都放行可见——宽松
    的历史数据兜底，代价是老会话谁都能看/删，但这批数据量小且是过渡态，
    换来的是不用为老数据编造一个可能错误的归属人。
    新会话一律带上创建者的 user_id，从这一刻起归属清晰。
    admin 角色不享有特权：同样只能看到自己名下 + 历史遗留会话，不做"管理员
    能看所有人会话"的例外——会话内容可能涉及提问者的敏感检索意图，默认给
    隐私而不是给管理可见性（有审计日志兜底追溯，不需要靠"admin 能翻会话"
    来补足运维能力）。
    user_id=None（API Key actor 或 auth=off 的合成 actor）时，
    `Conversation.user_id == None` 与 `.is_(None)` 等价，条件退化为只能看
    NULL 归属的会话——不会意外看到某个具体登录用户的会话。"""
    return or_(Conversation.user_id == user_id, Conversation.user_id.is_(None))


def create_conversation(sf, kb_id: str, user_id: str | None = None,
                        kb_ids: list[str] | None = None) -> dict:
    # M6-2：kb_ids 非空=多库会话（落 JSON）；None=单库（老行为，字节级不变）。
    conv = Conversation(id=str(uuid.uuid4()), kb_id=kb_id, user_id=user_id,
                        kb_ids=(json.dumps(kb_ids, ensure_ascii=False)
                                if kb_ids else None))
    with sf() as s:
        s.add(conv)
        s.commit()
    return {"id": conv.id, "kb_id": conv.kb_id, "kb_ids": kb_ids,
            "title": conv.title}


def conversation_kb_ids(sf, conv_id: str) -> list[str] | None:
    """M6-2：会话绑定的全部库 id（多库会话返回列表；单库/老会话返回 None）。"""
    with sf() as s:
        conv = s.get(Conversation, conv_id)
        if conv is None or not conv.kb_ids:
            return None
        try:
            return json.loads(conv.kb_ids) or None
        except (json.JSONDecodeError, TypeError):
            return None


def list_conversations(sf, kb_id: str | None = None, *,
                       limit: int = 30, offset: int = 0,
                       user_id: str | None = None) -> dict:
    """按 updated_at desc 分页；返回 {items, total}——total 为过滤后（按 kb_id
    与归属，见 _visible_filter）的总数，供前端判断是否还有更多可加载。"""
    with sf() as s:
        q = s.query(Conversation).filter(_visible_filter(user_id))
        if kb_id:
            q = q.filter_by(kb_id=kb_id)
        total = q.count()
        rows = (q.order_by(Conversation.updated_at.desc())
                .limit(limit).offset(offset).all())
        items = [{"id": c.id, "kb_id": c.kb_id, "title": c.title,
                 "updated_at": c.updated_at.isoformat()} for c in rows]
        return {"items": items, "total": total}


def get_conversation(sf, conv_id: str, user_id: str | None = None) -> Conversation | None:
    """归属过滤下查找单个会话；不可见（不存在 或 存在但归属别人）统一返回
    None——调用方（api/main.py）据此一律 404，不区分"真没有"与"有但不是你的"，
    避免把别人会话的存在性泄漏给探测请求。"""
    with sf() as s:
        return (s.query(Conversation)
                .filter(Conversation.id == conv_id, _visible_filter(user_id))
                .first())


def rename_conversation(sf, conv_id: str, title: str,
                        user_id: str | None = None) -> dict | None:
    with sf() as s:
        conv = (s.query(Conversation)
                .filter(Conversation.id == conv_id, _visible_filter(user_id))
                .first())
        if conv is None:
            return None
        conv.title = title
        conv.updated_at = datetime.utcnow()
        s.commit()
        return {"id": conv.id, "kb_id": conv.kb_id, "title": conv.title}


def delete_conversation(sf, conv_id: str, user_id: str | None = None) -> bool:
    """删除会话及其消息（无 ORM 级联，手工两步删，与 api/main.py delete_kb
    的既有级联删除手法一致）。归属过滤下找不到（不存在/不是你的）返回
    False，调用方 404。"""
    with sf() as s:
        conv = (s.query(Conversation)
                .filter(Conversation.id == conv_id, _visible_filter(user_id))
                .first())
        if conv is None:
            return False
        s.query(Message).filter_by(conv_id=conv_id).delete()
        s.delete(conv)
        s.commit()
        return True


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
