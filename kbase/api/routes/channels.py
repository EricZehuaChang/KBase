"""渠道身份映射的管理接口（T19）：绑定清单 / 新增（覆盖）/ 解绑。

权限：全部 require_admin。渠道绑定**决定"外部渠道里的某个人在 KBase 里是谁"**，
等价于把某个 KBase 账号的库级权限借给一个外部账号——这是授权动作，不是内容
编辑动作，editor 门槛不够（与 API Key 治理同一档）。

只读清单也要求 admin：清单里带 KBase 用户名与外部账号 id，是"谁在外部渠道里
代理了谁"的映射表，本身就是敏感信息。

不做前缀/通配绑定：一条绑定就是一个人（外部 id 精确匹配），不提供"整个飞书群
映射到某个账号"的写法——那种规则看着省事，实际是把一个人的权限发给一群人，
且事后无法从表里看出是谁在用（见 kbase/channels/core.py 的 resolve_actor）。
"""
from fastapi import Query, Request

from kbase.api.routes import RouteDeps
from kbase.api.schemas import ChannelIdentityBind
from kbase.api.services import Services
from kbase.channels import core as channels
from kbase.errors import AppError
from kbase.models import User

# 外部 id 长度上限（与 models.ChannelIdentity.external_user_id 的列宽一致）：
# 超长直接 422，不要让 DB 层抛截断错误——那样管理员看到的是 500，无从判断
# 是"id 太长"还是"服务出错"。
_EXTERNAL_ID_MAX = 200


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf = svc.sf

    def _guard_channel(channel: str) -> None:
        """渠道必须在册（kbase/channels/core.py 的 CHANNELS）。校验放在服务端
        而不是只在前端下拉里——否则脏值会写进表，而该渠道永远不会有事件进来，
        绑定悄悄失效却看着像"已经配好了"。"""
        if channel not in channels.CHANNELS:
            raise AppError("error.channel_unknown", "未知的渠道: {channel}",
                           status=422, channel=channel)

    @router.get("/channels", dependencies=[deps.require_admin])
    def list_channels():
        """在册渠道清单（管理端下拉用）。返回中文名便于界面直接显示，
        前端 i18n 仍可按 channel 码覆盖。"""
        return {"items": [{"channel": code, "label": label}
                          for code, label in channels.CHANNELS.items()]}

    @router.get("/channels/identities", dependencies=[deps.require_admin])
    def list_channel_identities(channel: str | None = Query(default=None)):
        """绑定清单（新→旧），可按渠道过滤。带 username/role 便于人读。

        顺带做一次一致性回收：绑定的用户已被删除时删掉那行孤儿绑定（用户的删除
        流程不经过本模块，见 models.ChannelIdentity 的 docstring）。读的时候收尾
        比在删除流程里挂钩子更可靠——不管用户是被哪个入口删的，下一次打开这张表
        都会自愈；不回收的话列表里会堆"用户缺失"的脏行，管理员无从判断该不该删。
        """
        if channel:
            _guard_channel(channel)
        channels.purge_orphans(sf)
        return {"items": channels.list_identities(sf, channel=channel)}

    @router.post("/channels/identities",
                 dependencies=[deps.require_admin, deps.audit_mutation])
    def bind_channel_identity(body: ChannelIdentityBind):
        """绑定（同一渠道内同一外部账号 = 改绑，见 channels.bind_identity）。

        校验用户存在：绑到一个不存在的 user_id 会让该外部账号永远走未映射
        兜底（resolve_actor 查不到用户即视为未映射），管理员却以为配好了——
        宁可 422 当场报错。
        """
        _guard_channel(body.channel)
        external_user_id = body.external_user_id.strip()
        if not external_user_id or len(external_user_id) > _EXTERNAL_ID_MAX:
            raise AppError("error.channel_external_id_invalid",
                           "外部账号 id 不合法（1~{max} 字符）",
                           status=422, max=_EXTERNAL_ID_MAX)
        with sf() as s:
            if s.get(User, body.user_id) is None:
                raise AppError("error.user_not_found", "用户不存在: {id}",
                               status=422, id=body.user_id)
        return channels.bind_identity(sf, channel=body.channel,
                                      external_user_id=external_user_id,
                                      user_id=body.user_id)

    @router.delete("/channels/identities/{identity_id}",
                   dependencies=[deps.require_admin, deps.audit_mutation])
    def unbind_channel_identity(identity_id: str, request: Request):
        """解绑：该外部账号立刻回落默认策略（未映射）。解绑**即时生效**——
        resolve_actor 每次问答都现查，不缓存（见 kbase/channels/core.py）。"""
        if not channels.unbind_identity(sf, identity_id):
            raise AppError("error.channel_identity_not_found",
                           "渠道绑定不存在: {id}", status=404, id=identity_id)
        return {"ok": True}
