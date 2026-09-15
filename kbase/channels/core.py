"""渠道适配层（T19）：把 IM 类入口（飞书机器人，将来钉钉/企微同构）收敛到
**一份**检索→可用依据→引用→拒答的编排上，并解决"外部账号是谁"的问题。

本模块只有两件事：

1. answer_for_channel —— 非流式的渠道问答。它与 routes/query.py 的 _run_query
   共享同一套语义（kb 策略解析、拒答阈值量纲随是否重排走、usable_blocks 只算
   一次供 citations 与回答共用、无可用依据即拒答短路），**不是另写一份**：两边
   都直接用 rs.resolve_strategy / Generator / usable_blocks / refusal_for 这几个
   组件。渠道自己写一套的代价是同一句提问在不同入口给出不同答案/不同拒答判断，
   归因数据横向一比就废了。
   渠道问答与网页问答的差别只有两点，都在这里显式处理：
   - 一次性出完整答案（IM 场景没有逐字流式的位置）；
   - 没有会话消息，归因行**没有 message_id**（所以也收不到点踩反馈，
     见 qa_outcomes.record_query_outcome 的 conv_id/message_id 说明）。

2. 渠道身份映射（channel_identities 表）：外部用户 id → KBase 用户。
   没有映射时 actor 退化成"匿名 viewer"（见 resolve_actor），库级可见性完全按
   公开/授权规则判定——**未映射不是"全放行"**：公开库（无 grant 行）照常可问，
   已收紧的库问不到（与登录用户的语义一致，见 kb_acl.can_access）。

越权/无权的处理与 _run_query 同一条：静默拒答（对外不区分"库不存在/无权/真没
答案"，防探测），对内落一行 bucket=scope_denied 的归因——渠道入口同样不能变成
权限旁路，也不能成为无记录的旁路。
"""
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi.concurrency import run_in_threadpool

from kbase import kb_acl
from kbase import qa_outcomes
from kbase import retrieval_strategy as rs
from kbase.models import ChannelIdentity, User
from kbase.rag.generator import Generator, refusal_for

# 现役渠道注册表：取值写死在这里（不是自由文本），管理端下拉与校验共用同一份。
# 加新渠道时：这里加一项 + 迁移对应机器人 → 归因表的 channel 列自动多一种取值
# （qa_outcomes 的 channel 不做白名单，历史行不会因为新增渠道而失效）。
# T20 加了 wecom（企业微信长连接智能机器人，见 kbase/channels/wecom.py）：
# 归因行的 channel 会开始出现 "wecom"，运营按渠道切片时能分出"这句话是从
# 企微来的还是飞书来的"——两个渠道的追问习惯不一样，缺口含义也不一样。
CHANNELS = {"feishu": "飞书", "wecom": "企业微信"}
DEFAULT_CHANNEL = "feishu"

# 未映射身份的角色：与渠道无关的**最低**权限档（viewer）。渠道入口拿不到登录态
# 的库级 scope，也不该凭空获得 editor/admin——它们只做问答，viewer 足够。
UNMAPPED_ROLE = "viewer"


@dataclass(frozen=True)
class ChannelActor:
    """渠道 Actor：answer_for_channel 的鉴权主体。

    name 是归因/审计里的 actor 名：已映射时用 KBase 用户名（与登录态问答同口径，
    运营在归因面板里能直接看出"这句话是谁问的"）；未映射时用
    "<渠道>:<外部 id>" ——不编一个查无此人的普通名字：外部 id 是唯一能回查
    "哪个飞书账号问的"的线索（open_id 本身不可反查真人，但同一个人反复提问会
    聚成同一个 actor）。
    """
    name: str
    role: str
    user_id: str | None
    channel: str
    external_user_id: str | None
    mapped: bool

    def as_actor_dict(self) -> dict:
        """转成 kb_acl 认的 actor 形状（与 auth/deps.py 组装的 actor 同构）。
        刻意**不带** scope_kb_ids：授权范围由 ACL（grants）决定，渠道映射只解决
        身份，不额外收窄范围——否则同一个人从网页能问的库在飞书里问不到，
        排查起来毫无线索。"""
        return {"name": self.name, "role": self.role,
                "user_id": self.user_id}


def _external_name(channel: str, external_user_id: str | None) -> str:
    """未映射身份的 actor 名。外部 id 缺失（事件里没有 sender）时退化成
    "<渠道>:unknown"：宁可名字难看，也不要和"没有身份"混为一谈。"""
    return f"{channel}:{external_user_id or 'unknown'}"


def resolve_actor(sf, *, channel: str,
                  external_user_id: str | None) -> ChannelActor:
    """外部账号 → 渠道 Actor。

    查找语义（恰好决定"同一句提问为什么两个人结果不同"）：
    1. 按 (channel, external_user_id) 精确查 channel_identities 一行；
    2. 有行 → 取 users.id 对应的用户，用**真实用户名与角色**（admin/editor/viewer
       与登录态完全一致，包括 admin 的 ACL 豁免）；
    3. 没有行、或行指向的用户已被删除/被停用 → 未映射：actor 名是
       "<渠道>:<外部 id>"，角色 viewer，user_id=None（=只看公开库：kb_acl 对
       user_id 为 NULL 的 actor 只放行无 grant 行的公开库，收紧过的库一律问不到）；
    4. external_user_id 为空（事件里没带 sender）→ 同上未映射。

    **不缓存**：一次问答一次查询，代价是一条主键索引上的点查；缓存的话"刚绑定的
    人还得等缓存过期才能问"（管理端刚保存就该生效），得不偿失。
    """
    if not external_user_id:
        return ChannelActor(name=_external_name(channel, external_user_id),
                            role=UNMAPPED_ROLE, user_id=None, channel=channel,
                            external_user_id=None, mapped=False)
    with sf() as s:
        row = (s.query(ChannelIdentity)
               .filter(ChannelIdentity.channel == channel,
                       ChannelIdentity.external_user_id == external_user_id)
               .first())
        if row is None:
            return ChannelActor(name=_external_name(channel, external_user_id),
                                role=UNMAPPED_ROLE, user_id=None, channel=channel,
                                external_user_id=external_user_id, mapped=False)
        user = s.get(User, row.user_id)
        if user is None or user.disabled:
            # 绑定的用户被删/被停用：等同未映射（默认策略），不静默沿用旧角色——
            # 停用账号还能通过渠道继续问答，等于停用没生效。
            return ChannelActor(name=_external_name(channel, external_user_id),
                                role=UNMAPPED_ROLE, user_id=None, channel=channel,
                                external_user_id=external_user_id, mapped=False)
        return ChannelActor(name=user.username, role=user.role, user_id=user.id,
                            channel=channel, external_user_id=external_user_id,
                            mapped=True)


async def answer_for_channel(svc, *, kb_id: str, question: str,
                             actor: ChannelActor,
                             provider: str | None = None,
                             top_k: int = 5) -> tuple[str, list[dict]]:
    """渠道问答（非流式）：返回 (答案, citations)。

    与 routes/query.py 的 _run_query 同一套语义：
    - KB 级检索策略（rs.resolve_strategy + 缺省全局默认）；
    - 拒答阈值量纲随"本次是否重排"走（rs.pick_min_score）；
    - usable_blocks 只算一次，citations 与生成共用同一份列表（引用编号与答案里的
      [n] 对齐）；
    - usable 为空 → 拒答文案（refusal_for，语言跟随提问）短路，不调 LLM；
    - 引用附图（多模态一期）照挂，IM 卡片才能带图。

    与 _run_query 的差异只有两处，都是渠道场景决定的：
    - 不流式：返回完整答案（IM 一次性出卡片）；
    - 无会话：不落消息、不回填 message_id、没有 conv_id。

    无权/越权（ACL 不过或 Key scope 不过）时静默拒答并落 scope_denied 归因：
    对外与"检索无依据"逐字一致（防探测），对内留下"谁问了哪个库但没权限"的
    安全记录——渠道入口不该是权限旁路，也不该是无记录的旁路。
    """
    # 鉴权优先于检索：越权询问压根不该触达检索器（否则既是信息泄漏面，也白烧
    # 一次向量检索）。判定与 /v1、KbGuard 完全同源（kb_acl 两条闸门都过）。
    identity = actor.as_actor_dict()
    if not (kb_acl.can_access(svc.sf, kb_id, identity)
            and kb_acl.scope_allows(identity, kb_id)):
        qa_outcomes.record_query_outcome(
            svc.sf, channel=actor.channel, kb_id=kb_id, question=question,
            blocks=[], usable=[],
            bucket=qa_outcomes.BUCKET_SCOPE_DENIED, actor=actor.name)
        return refusal_for(question), []

    llm = svc.get_llm(provider)
    strategy = rs.resolve_strategy(
        svc.cfg, rs.kb_retrieval_config(svc.sf, kb_id))
    min_score = rs.pick_min_score(svc.cfg, strategy,
                                  svc.retriever.rerank_active)
    blocks = await run_in_threadpool(
        svc.retriever.retrieve, kb_id, question, top_k, False, strategy)
    gen = Generator(llm, min_score=min_score,
                    min_include_score=svc.cfg.retrieval.min_include_score)
    usable = gen.usable_blocks(blocks)
    citations = gen.citations(usable)
    # 多模态回答（图片一期）：引用命中文本层 PDF 某页时附上该页插图。图片不进
    # prompt，零幻觉风险（与 _run_query 同一行代码，同一份实现）。
    from kbase.doc_images import attach_images
    attach_images(svc.sf, citations)

    # T12 归因：渠道入口同样一次问答一行，channel 记**真实渠道**（不是笼统的
    # "channel"）——同一批问题从不同入口进来，缺口含义不一样。
    qa_outcomes.record_query_outcome(
        svc.sf, channel=actor.channel, kb_id=kb_id, question=question,
        blocks=blocks, usable=usable, actor=actor.name)

    # 拒答：answer_stream 内部对 usable 为空短路（不调 LLM），这里逐字复用同一
    # 判定与文案——不能自己写一个 if 判空，否则"什么时候拒答"会有两份实现。
    if not usable:
        return refusal_for(question), []
    pieces = [p async for p in gen.answer_stream(question, usable, None)]
    return "".join(pieces), citations


# ---- 身份映射的读写（管理端接口与渠道解析共用一处判定） ----

def list_identities(sf, channel: str | None = None) -> list[dict]:
    """绑定清单（新→旧）。带 username：管理端要能人读"这是谁"，只给 user_id
    等于让管理员自己去用户表里对 uuid。"""
    with sf() as s:
        q = s.query(ChannelIdentity)
        if channel:
            q = q.filter(ChannelIdentity.channel == channel)
        rows = q.order_by(ChannelIdentity.created_at.desc()).all()
        users = {u.id: u for u in s.query(User).all()}
        out = []
        for r in rows:
            user = users.get(r.user_id)
            out.append({
                "id": r.id, "channel": r.channel,
                "external_user_id": r.external_user_id, "user_id": r.user_id,
                # 用户被删后行还留着（历史绑定），username 为 None 提示管理员清理
                "username": user.username if user else None,
                "role": user.role if user else None,
                "disabled": bool(user.disabled) if user else None,
                "created_at": r.created_at.isoformat()})
        return out


def bind_identity(sf, *, channel: str, external_user_id: str,
                  user_id: str) -> dict:
    """新增/覆盖绑定（同一渠道内一个外部账号只绑一个用户）。

    改绑 = 覆盖那一行而不是插新行：唯一约束下插会直接报错，而"先解绑再绑"是
    两步操作，中间那一刻这个人变成未映射（可能被策略放行/拦下都不是本意）。
    覆盖的语义也更符合直觉——管理员改的就是"这个人是谁"。

    调用方（路由）负责校验 channel 在册、user_id 存在；本函数只落库，不重复
    校验（判定只有一处，避免两处各写一份而漂移）。
    """
    with sf() as s:
        row = (s.query(ChannelIdentity)
               .filter(ChannelIdentity.channel == channel,
                       ChannelIdentity.external_user_id == external_user_id)
               .first())
        if row is None:
            row = ChannelIdentity(id=str(uuid.uuid4()), channel=channel,
                                  external_user_id=external_user_id,
                                  user_id=user_id,
                                  created_at=datetime.utcnow())
            s.add(row)
        else:
            row.user_id = user_id
        s.commit()
        return {"id": row.id, "channel": row.channel,
                "external_user_id": row.external_user_id, "user_id": row.user_id,
                "created_at": row.created_at.isoformat()}


def unbind_identity(sf, identity_id: str) -> bool:
    """解绑（删除该行）。解绑后该外部账号回落默认策略（未映射）。返回是否命中
    行——路由据此 404（不区分"不存在"与"不是你的"，渠道绑定是全局配置）。"""
    with sf() as s:
        row = s.get(ChannelIdentity, identity_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def purge_orphans(sf) -> int:
    """清理指向已删除用户的绑定行，返回删除条数。

    用户的删除流程（routes/admin.py 的 delete_user）不经过本模块，也不认这张表
    ——它只级联清理会话/消息/反馈/授权行。于是存在一个窄窗口：绑定行的 user_id
    指向已不存在的用户。**功能上无害**（resolve_actor 查不到用户即按未映射处理，
    不会越权），但列表里会堆"用户缺失"的脏行，管理员无从判断该不该删。读取路径
    顺手收尾（清单接口调用），比给用户删除流程加钩子更稳妥：不管人是从哪个入口
    删的，下一次打开这张表都会自愈。

    只删孤儿行，不动"用户被停用"的行：停用是可恢复状态（重新启用后绑定应当继续
    生效），列表要如实显示"已停用（按未绑定处理）"——那不是脏数据。
    """
    with sf() as s:
        user_ids = {row[0] for row in s.query(User.id).all()}
        stale = [r for r in s.query(ChannelIdentity).all()
                 if r.user_id not in user_ids]
        for r in stale:
            s.delete(r)
        if stale:
            s.commit()
        return len(stale)
