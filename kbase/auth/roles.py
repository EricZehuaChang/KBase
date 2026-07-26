"""角色与权限解析（自定义角色 RBAC）。

权限词表（v1，与现有路由门禁一一对应、真实强制）：
- content.manage —— 内容管理：建库/上传文档/连接器/分享链接/生成/评测等
  一切原 require_editor 门禁的端点；
- system.admin  —— 系统管理：设置组（Provider/用户/APIKey/发件箱/飞书/
  许可证/翻译）等一切原 require_admin 门禁的端点；
- audit.view    —— 审计与运营看板查看（审计日志/问答统计/无答案/反馈），
  从 admin 门禁中单拆——可造"只看审计不碰设置"的审计员角色。

内置四角色映射为权限集合（行为与 rank 时代逐字节一致）：viewer=∅（登录
即可问答），editor={content}，admin=全部；superadmin 在体系之外，代码里
特殊放行不走权限集合。自定义角色存 roles 表（仅存自定义，内置不落库），
permissions 为词表子集——删除某角色后仍挂着它的用户回落 ∅（=只剩问答）。
"""
import json

from kbase.models import RoleDef

PERM_CONTENT = "content.manage"
PERM_SYSTEM = "system.admin"
PERM_AUDIT = "audit.view"
PERMISSIONS: frozenset[str] = frozenset({PERM_CONTENT, PERM_SYSTEM, PERM_AUDIT})

# 内置角色 → 权限集合。superadmin 不在此表：它在 deps/各守卫处按角色名
# 特殊放行（体系之外），永远全权。
BUILTIN_ROLE_PERMS: dict[str, frozenset[str]] = {
    "viewer": frozenset(),
    "editor": frozenset({PERM_CONTENT}),
    "admin": PERMISSIONS,
    "superadmin": PERMISSIONS,
}

BUILTIN_ROLES: frozenset[str] = frozenset(BUILTIN_ROLE_PERMS)


def role_exists(sf, role: str) -> bool:
    """角色是否已定义（内置或 roles 表里有）。伪角色/已删角色返回 False——
    require_role 据此一律 403（连 viewer 门槛也不放行），与自定义角色引入前
    "未知角色全拒"的纵深防御一致。"""
    if role in BUILTIN_ROLE_PERMS:
        return True
    with sf() as s:
        return s.get(RoleDef, role) is not None


def resolve_permissions(sf, role: str) -> frozenset[str]:
    """角色名 → 权限集合。内置角色走常量零 DB；自定义角色查 roles 表
    （每请求一次小查询，自定义角色为少数派可接受）；查无此角色（已被删）
    回落空集=只剩问答，不炸请求。"""
    builtin = BUILTIN_ROLE_PERMS.get(role)
    if builtin is not None:
        return builtin
    with sf() as s:
        row = s.get(RoleDef, role)
        if row is None:
            return frozenset()
        try:
            perms = json.loads(row.permissions)
        except ValueError:
            return frozenset()
        return frozenset(p for p in perms if p in PERMISSIONS)
