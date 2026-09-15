"""问答归因（T12）：每次问答落一行 qa_outcomes，供运营看板按"桶"归因。

与审计表的分工（**两张表都写，互不替代**）：
- audit_logs：安全审计口径。拒答额外落 action="query_refused"，但只留问题前
  100 字、且不带会话/消息关联；既有看板（qa_stats.qa_overview /
  unanswered_questions）继续读它——T12 保留这条线，不删不改。
- qa_outcomes：运营归因口径。一次问答一行，带渠道/库/会话/消息/命中数/最高分，
  可按桶聚合、可下钻到当次问答与 citations、可导出。

桶是**互斥**的四选一（模块常量，取值写死在下面，不是自由文本；表注释与前端
i18n 都按这四个值对齐）：
- empty_retrieval —— 检索返回空。库里很可能压根没有这份资料（补文档）；
- below_threshold —— 检索到了东西，但最高分没过拒答阈值、没有可用依据。
  资料在库里却捞不起来（提问口径/切块/嵌入模型的问题，不是没资料）——
  与"检索为空"必须分开，两者的运营动作完全不同；
- scope_denied —— API Key 库级 scope 越权，静默空集。这是安全事件不是知识
  缺口（反问句："用户想问的库根本不让他问"）；
- answered —— 正常作答。

downvoted **不是桶**：它是 feedback=-1 叠加在原本的桶上（同一行既能 answered
又被点踩）。单列成桶会把"答砸了"的问题从它的归因桶里抹掉，反而丢信息。

数据只能从发布后开始积累：历史问答当时没有记录命中数/最高分，**无法回填**——
重跑历史问题拿到的是"今天的检索结果"，不是当时的事故现场，混进来只会污染
统计。所以第一份有意义的归因报告要等数据攒够（周量级），不存在"上线即见归因"。
"""
import json
import uuid
from datetime import datetime

from sqlalchemy import false, func, or_

from kbase.auth.deps import role_rank
from kbase.models import Message, QaOutcome, User

BUCKET_EMPTY_RETRIEVAL = "empty_retrieval"
BUCKET_BELOW_THRESHOLD = "below_threshold"
BUCKET_SCOPE_DENIED = "scope_denied"
BUCKET_ANSWERED = "answered"
# 元组顺序即看板展示顺序：先"能补的知识缺口"，再安全事件，最后正常作答
BUCKETS = (BUCKET_EMPTY_RETRIEVAL, BUCKET_BELOW_THRESHOLD,
           BUCKET_SCOPE_DENIED, BUCKET_ANSWERED)

# question 落库上限（与 models.QaOutcome 的注释一致）：归因行是给人看的缺口
# 线索，不是全文存储；截断只改长度不改语义。
_QUESTION_MAX_CHARS = 2000
# 导出上限：导出是人工排障/提标问用的，不做无限全量（响应体与内存都要有边界）。
EXPORT_MAX_ROWS = 5000
# CSV 列及顺序：路由层按它拼表头，测试按它断言列齐——列顺序只在这里定义一次。
EXPORT_COLUMNS = ("id", "ts", "channel", "kb_id", "kb_ids", "conv_id",
                  "message_id", "actor", "bucket", "retrieved_count",
                  "usable_count", "top_score", "question", "feedback")


def classify(retrieved_count: int, usable_count: int) -> str:
    """按检索结果定桶。

    scope_denied 不在此判定：越权静默空集发生在检索之前，根本没有 blocks，
    由入口层显式指定（见 record_query_outcome 的 bucket 参数）。
    """
    if usable_count:
        return BUCKET_ANSWERED
    return BUCKET_EMPTY_RETRIEVAL if not retrieved_count else BUCKET_BELOW_THRESHOLD


def summarize(blocks: list, usable: list) -> dict:
    """检索结果 → 归因行的三个量化字段。

    top_score 取本轮最高检索分；检索为空时为 None 而不是 0——0 会被读成
    "最高分就是 0"（阈值量纲随检索模式变，0 未必是低分）。
    """
    return {"retrieved_count": len(blocks), "usable_count": len(usable),
            "top_score": round(max(b.score for b in blocks), 3) if blocks else None}


def record_outcome(sf, *, channel: str, kb_id: str | None, bucket: str,
                   question: str, retrieved_count: int = 0,
                   usable_count: int = 0, top_score: float | None = None,
                   kb_ids: list[str] | None = None, conv_id: str | None = None,
                   actor: str | None = None) -> str:
    """落一行归因，返回行 id（会话轮次稍后用它回填 message_id）。

    返回 id 的原因：归因行写在生成之前（那时助手消息还没落库），消息 id 要等
    append_round 才拿得到，见 backfill_message_id。
    kb_ids 与 conversations.kb_ids 同约定：多库联查存 JSON 数组，单库 NULL。
    """
    row = QaOutcome(
        id=str(uuid.uuid4()), ts=datetime.utcnow(), channel=channel,
        kb_id=kb_id,
        kb_ids=(json.dumps(kb_ids, ensure_ascii=False) if kb_ids else None),
        conv_id=conv_id, actor=actor, bucket=bucket,
        retrieved_count=retrieved_count, usable_count=usable_count,
        top_score=top_score, question=question[:_QUESTION_MAX_CHARS])
    with sf() as s:
        s.add(row)
        s.commit()
    return row.id


def record_query_outcome(sf, *, channel: str, kb_id: str | None, question: str,
                         blocks: list, usable: list, bucket: str | None = None,
                         kb_ids: list[str] | None = None,
                         conv_id: str | None = None,
                         actor: str | None = None) -> str:
    """四个入口（web/share 的 _run_query、/v1、飞书机器人）唯一的写入入口：
    定量字段与定桶都收敛在这里。

    各入口各写一份的代价是同一个空结果在不同入口被记成不同桶（比如 /v1 只判
    "usable 为空"就一律记 below_threshold），报表横向一比就废了——所以定桶
    逻辑只能有一处。

    bucket 只在越权静默空集时显式传 BUCKET_SCOPE_DENIED：那种情况下压根没走到
    检索，"空"不代表库里没资料。
    """
    quant = summarize(blocks, usable)
    return record_outcome(
        sf, channel=channel, kb_id=kb_id,
        bucket=(bucket or classify(quant["retrieved_count"], quant["usable_count"])),
        question=question, kb_ids=kb_ids, conv_id=conv_id, actor=actor, **quant)


def backfill_message_id(sf, outcome_id: str, message_id: str) -> bool:
    """回填本轮助手消息 id（append_round 落库之后才拿得到），返回是否命中行。

    命中不了时静默 False：归因是旁路，不能因为回填失败把已经生成好的回答
    打成错误（用户看到的是回答，不是归因表的内部一致性）。
    """
    with sf() as s:
        n = (s.query(QaOutcome).filter(QaOutcome.id == outcome_id)
             .update({QaOutcome.message_id: message_id}))
        s.commit()
    return bool(n)


def sync_feedback(sf, message_id: str, feedback: int | None) -> int:
    """把反馈叠加到该消息的归因行（kbase/feedback.upsert_feedback 调用）。

    downvoted 不是桶，就靠这一步把"踩"叠加到原本的桶上；返回更新行数，
    非会话渠道（v1/飞书）没有 message_id，正常就是 0。
    """
    with sf() as s:
        n = (s.query(QaOutcome).filter(QaOutcome.message_id == message_id)
             .update({QaOutcome.feedback: feedback}))
        s.commit()
    return int(n)


def hidden_actors(sf, actor: dict | None) -> set[str] | None:
    """审计分层：非超管查看者需排除的 actor 集合（=全部超管用户名）；超管本人
    返回 None=看全量。

    与 routes/admin.py 的 _hidden_actors 同口径（那边是 register 内的闭包、
    无法直接复用；判定只有"是不是超管 + 超管用户名集合"两条，收在这里供
    归因端点使用，改动时两边一起看）。归因行同样带 actor，不过滤会从侧面
    泄漏超管的活动痕迹。
    """
    if role_rank((actor or {}).get("role", "")) >= role_rank("superadmin"):
        return None
    with sf() as s:
        return {u.username for u in
                s.query(User).filter_by(role="superadmin").all()}


def _json_list(raw: str | None) -> list | None:
    """JSON 数组列 → list。NULL=未配置返回 None；脏数据同样当"没有"返回 None，
    不因一行脏数据把归因端点打成 500（与 routes/admin.py 的 _json_list 同手法）。"""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, list) else None


def _row_out(r: QaOutcome) -> dict:
    """归因行的对外投影：字段名与 EXPORT_COLUMNS 一一对应（清单/下钻/导出
    看到同一套字段，避免某条通路偷偷少一列）。"""
    return {"id": r.id, "ts": r.ts.isoformat(), "channel": r.channel,
            "kb_id": r.kb_id, "kb_ids": _json_list(r.kb_ids),
            "conv_id": r.conv_id, "message_id": r.message_id, "actor": r.actor,
            "bucket": r.bucket, "retrieved_count": r.retrieved_count,
            "usable_count": r.usable_count, "top_score": r.top_score,
            "question": r.question, "feedback": r.feedback}


def _filtered(q, *, bucket: str | None, channel: str | None, kb_id: str | None,
              since: datetime | None, until: datetime | None,
              exclude_actors: set[str] | None,
              kb_ids_in: set[str] | None = None):
    """清单/导出/分布共用的过滤条件（三种读法口径必须完全一致，否则"分布说
    3 条、清单只有 2 条"这种对不上的账最招人烦）。

    kb_id 与 kb_ids_in 是两种不同的问题（都给了就同时生效）：
    - kb_id：运营在面板上选了"只看某个库"，精确匹配该行归属的库；
    - kb_ids_in：查看者**可见性**过滤——不带 kb_id 的清单要挡掉他无权看的库的
      行（与 routes/import_batches.py 逐行复核 ACL 同一手法，只是收敛成一条
      SQL 条件）。空集合=一条都不可见：`IN ()` 在 SQLAlchemy 上会退化成恒真，
      所以这里显式短路，避免"无权反而是看得最多"这种最坏的反转。
    """
    if bucket:
        q = q.filter(QaOutcome.bucket == bucket)
    if channel:
        q = q.filter(QaOutcome.channel == channel)
    if kb_id:
        q = q.filter(QaOutcome.kb_id == kb_id)
    if kb_ids_in is not None:
        q = q.filter(QaOutcome.kb_id.in_(sorted(kb_ids_in)) if kb_ids_in
                     else false())
    if since is not None:
        q = q.filter(QaOutcome.ts >= since)
    if until is not None:
        q = q.filter(QaOutcome.ts <= until)
    if exclude_actors:
        # actor 可为 NULL（无身份入口）：`NOT IN` 对 NULL 求值为 NULL，会把这些
        # 行一起滤掉，所以显式放行 NULL（它们是"匿名/未知"，不是超管）。
        q = q.filter(or_(QaOutcome.actor.is_(None),
                         ~QaOutcome.actor.in_(exclude_actors)))
    return q


def list_outcomes(sf, *, bucket: str | None = None, channel: str | None = None,
                  kb_id: str | None = None, since: datetime | None = None,
                  until: datetime | None = None, limit: int = 50,
                  offset: int = 0,
                  exclude_actors: set[str] | None = None,
                  kb_ids_in: set[str] | None = None) -> dict:
    """归因清单（新→旧）+ 同条件总数 + 按桶分布。

    buckets 分布**不叠加 bucket 过滤**（其余过滤照用）：看板要能"按渠道/库看
    四个桶各多少"再点进某一桶；分布若也按 bucket 过滤，就永远只看到自己那
    一格，等于没有分布。
    排序补一个 id desc 兜底：ts 在 SQLite 上刻度粗，同一刻多行时分页会跳行/
    重行（顺序本身无意义，稳定才是要求）。
    kb_ids_in 是查看者可见性过滤（见 _filtered），三处读法一致地生效。
    """
    with sf() as s:
        base = _filtered(s.query(QaOutcome), bucket=bucket, channel=channel,
                         kb_id=kb_id, since=since, until=until,
                         exclude_actors=exclude_actors, kb_ids_in=kb_ids_in)
        total = base.count()
        rows = (base.order_by(QaOutcome.ts.desc(), QaOutcome.id.desc())
                .limit(limit).offset(offset).all())
        dist = _filtered(s.query(QaOutcome.bucket, func.count(QaOutcome.id)),
                         bucket=None, channel=channel, kb_id=kb_id, since=since,
                         until=until, exclude_actors=exclude_actors,
                         kb_ids_in=kb_ids_in)
        counts = {b: 0 for b in BUCKETS}
        # 未知值（未来新增的桶）也照实带上，不要悄悄吞掉
        counts.update({str(b): int(c) for b, c in
                       dist.group_by(QaOutcome.bucket).all()})
        return {"items": [_row_out(r) for r in rows],
                "total": int(total), "buckets": counts}


def get_outcome(sf, outcome_id: str, *,
                exclude_actors: set[str] | None = None) -> dict | None:
    """单条下钻：归因行 + 该轮助手消息原文与 citations。

    非会话渠道（v1/飞书）没有 message_id，也就没有"助手消息"这回事——
    answer/citations 返回 None 而不是空串：空串会被读成"答了，但答案是空的"。
    exclude_actors 与清单同口径：超管行对非超管不可见，否则能按 id 直接捞出来。
    """
    with sf() as s:
        q = s.query(QaOutcome).filter(QaOutcome.id == outcome_id)
        if exclude_actors:
            q = q.filter(or_(QaOutcome.actor.is_(None),
                             ~QaOutcome.actor.in_(exclude_actors)))
        row = q.first()
        if row is None:
            return None
        out = {**_row_out(row), "answer": None, "citations": None}
        if row.message_id:
            msg = s.get(Message, row.message_id)
            if msg is not None and msg.role == "assistant":
                out["answer"] = msg.content
                out["citations"] = _json_list(msg.citations) or []
        return out


def export_rows(sf, *, bucket: str | None = None, channel: str | None = None,
                kb_id: str | None = None, since: datetime | None = None,
                until: datetime | None = None,
                exclude_actors: set[str] | None = None,
                kb_ids_in: set[str] | None = None) -> list[dict]:
    """导出用行（新→旧，上限 EXPORT_MAX_ROWS）。与 list_outcomes 同口径，
    只是不分页：CSV 是一次性产物，分页导出只会让人拿到半份数据还以为全了。
    kb_ids_in 一并透传：导出与清单必须"看到同一批行"，否则导出来的 CSV 比
    面板上多（或少）几行，对账时无从解释。"""
    return list_outcomes(sf, bucket=bucket, channel=channel, kb_id=kb_id,
                         since=since, until=until, limit=EXPORT_MAX_ROWS,
                         exclude_actors=exclude_actors,
                         kb_ids_in=kb_ids_in)["items"]
