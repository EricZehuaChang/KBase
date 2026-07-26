"""管理域路由：用户管理、自定义角色、API Key、审计查询、许可证状态（spec §3/§5，G3）。"""
import json
import secrets as _secrets
import uuid

from fastapi import BackgroundTasks, Query, Request

from kbase import qa_stats
from kbase.api.routes import RouteDeps
from kbase.api.schemas import (ApiKeyCreate, InviteBody, RoleCreate, RoleUpdate,
                               UserCreate, UserUpdate)
from kbase.api.services import Services
from kbase.audit import list_audit
from kbase.auth import security
from kbase.auth.deps import role_rank
from kbase.errors import AppError
from kbase.license import check_license
from kbase.auth import roles as auth_roles
from kbase.models import (ApiKey, Conversation, KbGrant, Message,
                          MessageFeedback, RoleDef, User)


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf = svc.sf

    def _hidden_actors(request: Request) -> set[str] | None:
        """审计分层：非超管查看者需排除的 actor 集合（=全部超管用户名，按
        当前角色动态解析）；超管本人返回 None=看全量。审计如实落库不删改，
        只在读取侧按查看者分层。"""
        if _actor_is_super(request):
            return None
        with sf() as s:
            return {u.username for u in
                    s.query(User).filter_by(role="superadmin").all()}

    @router.get("/audit", dependencies=[deps.require_admin])
    def audit_list(request: Request,
                   limit: int = Query(default=50, ge=1, le=200),
                   offset: int = Query(default=0, ge=0)):
        return list_audit(sf, limit=limit, offset=offset,
                          exclude_actors=_hidden_actors(request))

    # ---- 运营看板（C）：问答量/拒答率 + 无答案问题清单 ----

    @router.get("/stats/qa", dependencies=[deps.require_admin])
    def stats_qa(days: int = Query(default=7, ge=1, le=90)):
        return qa_stats.qa_overview(sf, days=days)

    @router.get("/stats/unanswered", dependencies=[deps.require_admin])
    def stats_unanswered(request: Request,
                         limit: int = Query(default=50, ge=1, le=200)):
        """无答案（拒答）问题清单——运营看'用户问了什么答不上'补知识。
        清单出自审计表且带 actor，超管行同样按查看者分层过滤。"""
        return {"items": qa_stats.unanswered_questions(
            sf, limit=limit, exclude_actors=_hidden_actors(request))}

    @router.get("/stats/feedback", dependencies=[deps.require_admin])
    def stats_feedback(limit: int = Query(default=50, ge=1, le=200)):
        """M6-4 反馈闭环看板：赞/踩总量 + 差评清单（带问题原文与答案摘录）。
        与无答案清单互补：拒答=答不上，差评=答了但答砸了。"""
        from kbase import feedback
        return {**feedback.feedback_stats(sf),
                "items": feedback.negative_list(sf, limit=limit)}

    @router.post("/settings/api-keys",
                 dependencies=[deps.require_admin, deps.audit_mutation])
    def create_api_key(body: ApiKeyCreate):
        full_key, prefix, key_hash = security.generate_api_key()
        row = ApiKey(id=str(uuid.uuid4()), name=body.name, prefix=prefix,
                    key_hash=key_hash, role=body.role, revoked=False)
        with sf() as s:
            s.add(row)
            s.commit()
        return {"id": row.id, "name": row.name, "role": row.role, "key": full_key}

    @router.get("/settings/api-keys", dependencies=[deps.require_admin])
    def list_api_keys():
        # 完整 key 与 key_hash 都不返回——hash 不该暴露给客户端，完整 key
        # 只在创建的那一刻返回一次（见 create_api_key）。
        with sf() as s:
            rows = s.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
            return [{"id": r.id, "name": r.name, "prefix": r.prefix,
                     "role": r.role, "revoked": r.revoked,
                     "created_at": r.created_at.isoformat()} for r in rows]

    @router.delete("/settings/api-keys/{key_id}",
                   dependencies=[deps.require_admin, deps.audit_mutation])
    def revoke_api_key(key_id: str):
        # 软删除：吊销后 Bearer 通道立即拒绝（get_current_actor 校验 revoked
        # 字段，见 kbase/auth/deps.py），但保留行本身供审计/历史查询。
        with sf() as s:
            row = s.get(ApiKey, key_id)
            if row is None:
                raise AppError("error.apikey_not_found", "API Key 不存在: {id}", status=404, id=key_id)
            row.revoked = True
            s.commit()
        return {"ok": True}

    def _user_out(u: User) -> dict:
        # 从不返回 password_hash——列表/创建/更新的响应体统一走这个投影。
        return {"id": u.id, "username": u.username, "email": u.email,
                "role": u.role, "disabled": u.disabled,
                "advanced_ui": bool(u.advanced_ui),
                "created_at": u.created_at.isoformat()}

    @router.get("/users", dependencies=[deps.require_admin])
    def list_users(request: Request):
        with sf() as s:
            rows = s.query(User).order_by(User.created_at.asc()).all()
            # 超管在管理体系之外：普通 admin 的用户列表里根本不出现超管
            # 账号（不可见，而非可见但锁定）——外发演示 admin 无从得知
            # 系统 owner 账号的存在。
            if not _actor_is_super(request):
                rows = [u for u in rows if u.role != "superadmin"]
            return [_user_out(u) for u in rows]

    def _actor_is_super(request: Request) -> bool:
        actor = getattr(request.state, "actor", None) or {}
        return role_rank(actor.get("role", "")) >= role_rank("superadmin")

    def _validate_role(role: str) -> None:
        """角色必须是内置四角色或已存在的自定义角色（UserRole 放开为 str 后，
        由这里兜住非法值——否则用户会挂上一个谁也没定义的角色而失去权限）。"""
        if role in auth_roles.BUILTIN_ROLES:
            return
        with sf() as s:
            if s.get(RoleDef, role) is None:
                raise AppError("error.role_not_found", "角色不存在: {name}",
                               status=422, name=role)

    # ---- 自定义角色（仅超管）----

    @router.get("/roles", dependencies=[deps.require_admin])
    def list_roles():
        """角色清单：内置四角色（builtin=true，权限只读）+ 自定义角色。
        前端角色下拉与角色管理卡片共用。"""
        out = [{"name": name, "label": "", "builtin": True,
                "permissions": sorted(perms)}
               for name, perms in auth_roles.BUILTIN_ROLE_PERMS.items()]
        with sf() as s:
            for r in s.query(RoleDef).order_by(RoleDef.created_at.asc()).all():
                try:
                    perms = sorted(p for p in json.loads(r.permissions)
                                   if p in auth_roles.PERMISSIONS)
                except ValueError:
                    perms = []
                out.append({"name": r.name, "label": r.label, "builtin": False,
                            "permissions": perms})
        return {"roles": out, "permissions": sorted(auth_roles.PERMISSIONS)}

    @router.post("/roles", dependencies=[deps.require_admin, deps.audit_mutation])
    def create_role(body: RoleCreate, request: Request):
        if not _actor_is_super(request):
            raise AppError("error.superadmin_required",
                           "该操作仅超级管理员可执行", status=403)
        if body.name in auth_roles.BUILTIN_ROLES:
            raise AppError("error.role_builtin", "内置角色不可创建/修改/删除",
                           status=422)
        perms = [p for p in body.permissions if p in auth_roles.PERMISSIONS]
        with sf() as s:
            if s.get(RoleDef, body.name) is not None:
                raise AppError("error.role_exists", "角色已存在: {name}",
                               status=409, name=body.name)
            s.add(RoleDef(name=body.name, label=body.label.strip(),
                          permissions=json.dumps(perms)))
            s.commit()
        return {"name": body.name, "label": body.label.strip(),
                "builtin": False, "permissions": sorted(perms)}

    @router.put("/roles/{name}", dependencies=[deps.require_admin, deps.audit_mutation])
    def update_role(name: str, body: RoleUpdate, request: Request):
        if not _actor_is_super(request):
            raise AppError("error.superadmin_required",
                           "该操作仅超级管理员可执行", status=403)
        if name in auth_roles.BUILTIN_ROLES:
            raise AppError("error.role_builtin", "内置角色不可创建/修改/删除",
                           status=422)
        with sf() as s:
            row = s.get(RoleDef, name)
            if row is None:
                raise AppError("error.role_not_found", "角色不存在: {name}",
                               status=404, name=name)
            if body.label is not None:
                row.label = body.label.strip()
            if body.permissions is not None:
                row.permissions = json.dumps(
                    [p for p in body.permissions if p in auth_roles.PERMISSIONS])
            s.commit()
            return {"name": row.name, "label": row.label, "builtin": False,
                    "permissions": sorted(json.loads(row.permissions))}

    @router.delete("/roles/{name}",
                   dependencies=[deps.require_admin, deps.audit_mutation])
    def delete_role(name: str, request: Request):
        """删除自定义角色：仍在使用该角色的用户会失去全部权限（只剩问答），
        故有用户在用时拒绝删除，先改派再删。"""
        if not _actor_is_super(request):
            raise AppError("error.superadmin_required",
                           "该操作仅超级管理员可执行", status=403)
        if name in auth_roles.BUILTIN_ROLES:
            raise AppError("error.role_builtin", "内置角色不可创建/修改/删除",
                           status=422)
        with sf() as s:
            row = s.get(RoleDef, name)
            if row is None:
                raise AppError("error.role_not_found", "角色不存在: {name}",
                               status=404, name=name)
            in_use = s.query(User).filter_by(role=name).count()
            if in_use:
                raise AppError("error.role_in_use",
                               "仍有 {n} 个用户在使用该角色，请先改派",
                               status=422, n=in_use)
            s.delete(row)
            s.commit()
        return {"ok": True}

    @router.post("/users", dependencies=[deps.require_admin, deps.audit_mutation])
    def create_user(body: UserCreate, request: Request, bg: BackgroundTasks):
        # 超管层级在管理体系之外：普通 admin 不能创建 superadmin 账号
        if body.role == "superadmin" and not _actor_is_super(request):
            raise AppError("error.superadmin_only",
                           "仅超级管理员可创建/管理超级管理员账号", status=403)
        _validate_role(body.role)      # 自定义角色须已存在（UserRole 已放开为 str）
        with sf() as s:
            if s.query(User).filter_by(username=body.username).first() is not None:
                raise AppError("error.username_exists", "用户名已存在: {name}", status=409, name=body.username)
            user = User(id=str(uuid.uuid4()), username=body.username,
                       email=(body.email or None),
                       password_hash=security.hash_password(body.password),
                       role=body.role, disabled=False,
                       advanced_ui=bool(body.advanced_ui))
            s.add(user)
            s.commit()
            s.refresh(user)
            out = _user_out(user)
        # 账号通知邮件：填了邮箱且发件箱已配置 → 后台发送（发信失败只落
        # 日志，不影响建号——邮件是通知增强，不是建号的硬依赖）
        if body.email:
            from kbase import email_templates, mailer
            if mailer.status(sf)["configured"]:
                login_url = str(request.base_url).rstrip("/")

                def _notify(to=body.email, username=body.username,
                            password=body.password, url=login_url):
                    import logging as _logging
                    try:
                        subject, text, html_body = \
                            email_templates.account_created(username, password, url)
                        mailer.send_mail(sf, to, subject, text, html=html_body)
                    except Exception as e:  # noqa: BLE001
                        _logging.getLogger(__name__).warning(
                            "账号通知邮件发送失败（%s）: %s", to, e)
                bg.add_task(_notify)
        return out

    @router.put("/users/{user_id}", dependencies=[deps.require_admin, deps.audit_mutation])
    def update_user(user_id: str, body: UserUpdate, request: Request):
        with sf() as s:
            user = s.get(User, user_id)
            if user is None:
                raise AppError("error.user_not_found", "用户不存在: {id}", status=404, id=user_id)

            # 超管层级在管理体系之外：超管账号对普通 admin **不可见**（列表
            # 已过滤），修改也按"不存在"回 404——403 会泄漏"存在但无权"，
            # 与全仓不泄露存在性的原则一致（同 conv/kb 的 404 语义）。
            if user.role == "superadmin" and not _actor_is_super(request):
                raise AppError("error.user_not_found", "用户不存在: {id}",
                               status=404, id=user_id)
            # 把（可见的）普通用户提为 superadmin 仍只有超管能做——目标存在
            # 且可见，这里如实回 403。
            if body.role == "superadmin" and not _actor_is_super(request):
                raise AppError("error.superadmin_only",
                               "仅超级管理员可创建/管理超级管理员账号", status=403)

            # 不变量：不能让"启用中的最高层级"清零，否则系统失去最高管理权。
            # 用变更后的假想状态计算，disabled=True 与 role 降级两条触发路径
            # 共用同一条校验。有超管的库守超管数；无超管的存量库退守旧规则
            # （启用 admin 数不清零），防迁移遗漏时锁死系统。
            would_be_role = body.role if body.role is not None else user.role
            would_be_disabled = (body.disabled if body.disabled is not None
                                 else user.disabled)

            def _others_enabled(role: str) -> int:
                return (s.query(User)
                        .filter(User.id != user_id, User.role == role,
                                User.disabled == False)  # noqa: E712
                        .count())

            is_super_now = user.role == "superadmin" and not user.disabled
            would_remain_super = (would_be_role == "superadmin"
                                  and not would_be_disabled)
            if is_super_now and not would_remain_super:
                if _others_enabled("superadmin") == 0:
                    raise AppError("error.last_superadmin",
                                   "不能禁用/降级最后一个超级管理员", status=422)
            has_any_super = (s.query(User)
                             .filter(User.role == "superadmin",
                                     User.disabled == False)  # noqa: E712
                             .count() > 0)
            is_admin_now = user.role == "admin" and not user.disabled
            would_remain_admin = would_be_role == "admin" and not would_be_disabled
            if (not has_any_super and is_admin_now and not would_remain_admin
                    and _others_enabled("admin") == 0):
                raise AppError("error.last_admin", "不能禁用/降级最后一个管理员", status=422)

            # 改账号名（仅超管）：JWT sub=用户名，改名后该用户旧会话下一次
            # 请求即 401 须重新登录；历史审计行保留旧名（如实记录不回写）。
            if body.username is not None:
                new_name = body.username.strip()
                if not _actor_is_super(request):
                    raise AppError("error.superadmin_required",
                                   "该操作仅超级管理员可执行", status=403)
                if new_name and new_name != user.username:
                    if (s.query(User).filter_by(username=new_name).first()
                            is not None):
                        raise AppError("error.username_exists", "用户名已存在: {name}",
                                       status=409, name=new_name)
                    user.username = new_name
            if body.role is not None:
                _validate_role(body.role)
                user.role = body.role
            if body.disabled is not None:
                user.disabled = body.disabled
            if body.password is not None:
                user.password_hash = security.hash_password(body.password)
            if body.email is not None:
                user.email = body.email or None   # 空串=清除邮箱
            if body.advanced_ui is not None:
                user.advanced_ui = body.advanced_ui
            s.commit()
            s.refresh(user)
            return _user_out(user)

    @router.delete("/users/{user_id}",
                   dependencies=[deps.require_admin, deps.audit_mutation])
    def delete_user(user_id: str, request: Request):
        """删除账号（仅超管；admin 只能禁用=软路径）。连带清理其**私有**数据：
        会话/消息/消息反馈/库授权行；团队资产（知识库/文档）不随人删——
        knowledge_bases.owner_id 悬空仅失去 owner 豁免，库按 grants/公开规则
        照常可用。审计历史保留旧用户名（如实记录）。不变量：不能删除最后
        一个启用超管（防锁死，与禁用/降级同一条线）。"""
        if not _actor_is_super(request):
            raise AppError("error.superadmin_required",
                           "该操作仅超级管理员可执行", status=403)
        with sf() as s:
            user = s.get(User, user_id)
            if user is None:
                raise AppError("error.user_not_found", "用户不存在: {id}",
                               status=404, id=user_id)
            if user.role == "superadmin" and not user.disabled:
                others = (s.query(User)
                          .filter(User.id != user_id, User.role == "superadmin",
                                  User.disabled == False)  # noqa: E712
                          .count())
                if others == 0:
                    raise AppError("error.last_superadmin",
                                   "不能禁用/降级最后一个超级管理员", status=422)
            uid = user.id
            # 私有数据级联（顺序：反馈→消息→会话→授权行，避免悬挂引用）
            conv_ids = [c.id for c in
                        s.query(Conversation).filter_by(user_id=uid).all()]
            if conv_ids:
                s.query(MessageFeedback).filter(
                    MessageFeedback.conv_id.in_(conv_ids)).delete(
                    synchronize_session=False)
                s.query(Message).filter(
                    Message.conv_id.in_(conv_ids)).delete(
                    synchronize_session=False)
                s.query(Conversation).filter(
                    Conversation.user_id == uid).delete(
                    synchronize_session=False)
            s.query(KbGrant).filter(KbGrant.user_id == uid).delete(
                synchronize_session=False)
            s.delete(user)
            s.commit()
        return {"ok": True}

    @router.post("/users/{user_id}/invite",
                 dependencies=[deps.require_admin, deps.audit_mutation])
    def invite_user(user_id: str, body: InviteBody, request: Request):
        """邀请/重发凭据（「邮箱与邀请」对话框）：可顺带维护邮箱，为账号设置
        新初始密码（给定或随机生成），发送"登录地址+账号+初始密码"邮件（按
        账号语言偏好选中/英文模板）。**同步发送**——失败直接报给管理员（邀请
        的全部意义就是发信，不静默吞）；先发信后落库，发信失败不动密码。"""
        from kbase import email_templates, mailer
        if not mailer.status(sf)["configured"]:
            raise AppError("error.smtp_unconfigured",
                           "发件箱未配置（设置 → 系统 → 发件箱）", status=422)
        with sf() as s:
            user = s.get(User, user_id)
            if user is None:
                raise AppError("error.user_not_found", "用户不存在: {id}",
                               status=404, id=user_id)
            # 超管对普通 admin 不可见：按"不存在"处理（与 update_user 同语义）
            if user.role == "superadmin" and not _actor_is_super(request):
                raise AppError("error.user_not_found", "用户不存在: {id}",
                               status=404, id=user_id)
            email = (body.email or "").strip() or (user.email or "")
            if not email:
                raise AppError("error.invite_needs_email",
                               "该用户未设置邮箱，请先填写邮箱", status=422)
            username, lang = user.username, user.language
        password = (body.password or "").strip() or _secrets.token_urlsafe(9)
        login_url = str(request.base_url).rstrip("/")
        subject, text, html_body = email_templates.account_invite(
            username, password, login_url, lang=lang)
        try:
            mailer.send_mail(sf, email, subject, text, html=html_body)
        except Exception as e:  # noqa: BLE001
            raise AppError("error.invite_send_failed", "邀请邮件发送失败：{msg}",
                           status=502, msg=str(e)[:200]) from e
        # 发信成功才落库：邮件里的初始密码与 DB 哈希保持一致
        with sf() as s:
            user = s.get(User, user_id)
            user.email = email
            user.password_hash = security.hash_password(password)
            s.commit()
        return {"ok": True, "email": email}

    @router.get("/license", dependencies=[deps.require_viewer])
    def get_license():
        return check_license()
