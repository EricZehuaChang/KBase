"""库级权限（M6-3）：某用户能访问哪些知识库。

模型（"不配就公开，一配就收紧"，与检索策略同哲学，向后兼容）：
- admin：全部可见（管理员豁免）；
- 某 KB 没有任何 grant 行 → 公开，所有登录用户可见（老库/未配即老行为）；
- 某 KB 有 grant 行 → 仅 grant 内 user_id + owner_id + admin 可见。

principal 目前只到 user_id 级（role/部门级留后续）；auth=off（合成 admin）
下天然全通，既有功能测试不受影响。
"""
import uuid

from kbase.models import KbGrant, KnowledgeBase, User


def _is_admin(actor: dict) -> bool:
    # superadmin 是 admin 的超集：ACL 豁免同样适用（auth=off 的合成 actor
    # 也已改为 superadmin，见 deps.make_synthetic_admin_actor_dependency）
    return actor.get("role") in ("admin", "superadmin")


def visible_kb_filter(sf, actor: dict):
    """返回 (mode, kb_id_set)：("all", None)=全部可见（admin）；
    ("set", {...})=只可见集合内的 kb_id。list_kb 据此批量过滤。"""
    if _is_admin(actor):
        return ("all", None)
    uid = actor.get("user_id")
    with sf() as s:
        all_ids = {row[0] for row in s.query(KnowledgeBase.id).all()}
        restricted = {row[0] for row in s.query(KbGrant.kb_id).distinct().all()}
        granted, owned = set(), set()
        if uid is not None:
            granted = {row[0] for row in
                       s.query(KbGrant.kb_id).filter(KbGrant.user_id == uid).all()}
            owned = {row[0] for row in s.query(KnowledgeBase.id)
                     .filter(KnowledgeBase.owner_id == uid).all()}
    public = all_ids - restricted           # 无 grant 的库 = 公开
    return ("set", public | granted | owned)


def scope_allows(actor: dict, kb_id: str) -> bool:
    """API Key 库级 scope 判定：actor 是否被白名单允许访问 kb_id。

    T01/G01：scope 与 ACL 是两条独立的闸门，必须分别判定——
    - actor 没有 scope_kb_ids（Cookie 通道 / 未设 scope 的 key / auth=off 的
      合成 actor）→ 恒 True，行为与升级前一致；
    - 有 scope_kb_ids（受限 API Key）→ 仅白名单内的 kb_id 通过。

    **不要把它合并进 can_access**：原生入口依赖两种不同语义——ACL 挡=404
    （不泄漏存在性），scope 挡=静默空集（/search 空集、/query 拒答流，
    见 tests/test_apikey_scope.py）。另注意 scope 判定必须独立于角色：
    API Key 可以是 admin 角色（kb_acl._is_admin 会对 admin 豁免 ACL），
    受限的 admin key 仍然只能访问白名单内的库，所以这里不看 role。
    """
    scope = actor.get("scope_kb_ids") if actor else None
    return scope is None or kb_id in scope


def apply_scope(actor: dict, kb_ids) -> set:
    """把一批 kb_id 过一遍 scope，返回允许的集合（T01/G01：/v1/models 用）。
    无 scope 的 actor 原样返回全部（与 scope_allows 同一判定）。"""
    ids = list(kb_ids)
    if (actor.get("scope_kb_ids") if actor else None) is None:
        return set(ids)
    return {k for k in ids if scope_allows(actor, k)}


def can_access(sf, kb_id: str, actor: dict) -> bool:
    """单库访问判定（检索/问答/文档操作前置校验）。"""
    if _is_admin(actor):
        return True
    with sf() as s:
        kb = s.get(KnowledgeBase, kb_id)
        if kb is None:
            return False
        uid = actor.get("user_id")
        if uid is not None and kb.owner_id == uid:
            return True
        grant_count = s.query(KbGrant).filter(KbGrant.kb_id == kb_id).count()
        if grant_count == 0:
            return True                     # 公开库
        if uid is None:
            return False
        return (s.query(KbGrant)
                .filter(KbGrant.kb_id == kb_id, KbGrant.user_id == uid)
                .count() > 0)


def list_grants(sf, kb_id: str) -> list[dict]:
    """某库的授权用户清单（管理端展示），含用户名便于人读。"""
    with sf() as s:
        rows = (s.query(KbGrant, User)
                .outerjoin(User, User.id == KbGrant.user_id)
                .filter(KbGrant.kb_id == kb_id)
                .order_by(KbGrant.created_at.asc()).all())
        return [{"user_id": g.user_id,
                 "username": (u.username if u else None),
                 "created_at": g.created_at.isoformat()} for g, u in rows]


def set_grants(sf, kb_id: str, user_ids: list[str]) -> None:
    """全量覆盖某库的授权用户集合（空列表=恢复公开）。"""
    with sf() as s:
        s.query(KbGrant).filter(KbGrant.kb_id == kb_id).delete()
        for uid in dict.fromkeys(user_ids):     # 去重保序
            s.add(KbGrant(id=str(uuid.uuid4()), kb_id=kb_id, user_id=uid))
        s.commit()
