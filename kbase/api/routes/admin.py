"""管理域路由：用户管理、API Key、审计查询、许可证状态（spec §3/§5，G3）。"""
import uuid

from fastapi import BackgroundTasks, Query, Request

from kbase import qa_stats
from kbase.api.routes import RouteDeps
from kbase.api.schemas import ApiKeyCreate, UserCreate, UserUpdate
from kbase.api.services import Services
from kbase.audit import list_audit
from kbase.auth import security
from kbase.errors import AppError
from kbase.license import check_license
from kbase.models import ApiKey, User


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf = svc.sf

    @router.get("/audit", dependencies=[deps.require_admin])
    def audit_list(limit: int = Query(default=50, ge=1, le=200),
                  offset: int = Query(default=0, ge=0)):
        return list_audit(sf, limit=limit, offset=offset)

    # ---- 运营看板（C）：问答量/拒答率 + 无答案问题清单 ----

    @router.get("/stats/qa", dependencies=[deps.require_admin])
    def stats_qa(days: int = Query(default=7, ge=1, le=90)):
        return qa_stats.qa_overview(sf, days=days)

    @router.get("/stats/unanswered", dependencies=[deps.require_admin])
    def stats_unanswered(limit: int = Query(default=50, ge=1, le=200)):
        """无答案（拒答）问题清单——运营看'用户问了什么答不上'补知识。"""
        return {"items": qa_stats.unanswered_questions(sf, limit=limit)}

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
    def list_users():
        with sf() as s:
            rows = s.query(User).order_by(User.created_at.asc()).all()
            return [_user_out(u) for u in rows]

    @router.post("/users", dependencies=[deps.require_admin, deps.audit_mutation])
    def create_user(body: UserCreate, request: Request, bg: BackgroundTasks):
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
    def update_user(user_id: str, body: UserUpdate):
        with sf() as s:
            user = s.get(User, user_id)
            if user is None:
                raise AppError("error.user_not_found", "用户不存在: {id}", status=404, id=user_id)

            # 不变量：不能让"启用中的 admin"数量降到 0——无论是禁用最后一个
            # 启用 admin，还是把最后一个启用 admin 降级成非 admin。用变更后
            # 的假想状态计算启用 admin 数，而不是分别判断字段，这样两种触发
            # 路径（disabled=True 或 role=非admin）共用同一条校验。
            would_be_role = body.role if body.role is not None else user.role
            would_be_disabled = (body.disabled if body.disabled is not None
                                 else user.disabled)
            is_admin_now = user.role == "admin" and not user.disabled
            would_remain_admin = would_be_role == "admin" and not would_be_disabled
            if is_admin_now and not would_remain_admin:
                other_enabled_admins = (
                    s.query(User)
                    .filter(User.id != user_id, User.role == "admin",
                           User.disabled == False)  # noqa: E712
                    .count())
                if other_enabled_admins == 0:
                    raise AppError("error.last_admin", "不能禁用/降级最后一个管理员", status=422)

            if body.role is not None:
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

    @router.get("/license", dependencies=[deps.require_viewer])
    def get_license():
        return check_license()
