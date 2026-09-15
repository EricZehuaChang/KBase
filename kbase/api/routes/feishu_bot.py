"""飞书群机器人路由（对标 #2）：管理配置（admin）+ 事件回调（公开）。

事件回调必须 3 秒内响应，检索+生成放 BackgroundTasks 异步执行、完成后
调飞书 reply 接口——与免登录分享同一"公开端点+后台重活"结构。
安全：加密模式验签+解密；明文模式核对 verification token；两者都不过=403。

T19：检索+生成换成渠道适配层（kbase/channels/core.py 的 answer_for_channel），
本文件不再自己拼 rs.resolve_strategy/Generator——渠道入口与网页问答共用同一套
检索/拒答语义，机器人只负责"事件怎么解析、卡片怎么回"。协议层（验签/解密/
握手/去重/解析）一行未动。
"""
import json
import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from kbase import feishu, feishu_bot
from kbase.api.routes import RouteDeps
from kbase.api.schemas import FeishuBotSettingsBody
from kbase.api.services import Services
from kbase.audit import write_audit
from kbase.channels import core as channels
from kbase.errors import AppError
from kbase.models import KnowledgeBase

logger = logging.getLogger(__name__)

# T19 渠道名（归因行的 channel 取值来源；注册表见 channels.core.CHANNELS）
CHANNEL = "feishu"


def register(app: FastAPI, router, svc: Services, deps: RouteDeps) -> None:
    sf = svc.sf

    # ---- 管理组（admin，设置页"连接器"区块） ----

    @router.get("/settings/feishu-bot", dependencies=[deps.require_admin])
    def get_bot_settings():
        return feishu_bot.status(sf)

    @router.put("/settings/feishu-bot",
                dependencies=[deps.require_admin, deps.audit_mutation])
    def put_bot_settings(body: FeishuBotSettingsBody):
        with sf() as s:
            if s.get(KnowledgeBase, body.kb_id) is None:
                raise AppError("error.kb_not_found", "知识库不存在: {id}", status=422, id=body.kb_id)
        feishu_bot.set_settings(sf, verification_token=body.verification_token,
                                encrypt_key=body.encrypt_key,
                                kb_id=body.kb_id, provider=body.provider)
        return feishu_bot.status(sf)

    # ---- 后台：检索+生成+回复（非流式——IM 场景一次性出完整答案） ----

    async def _answer_and_reply(question: str, message_id: str,
                                external_user_id: str | None) -> None:
        """一条群消息 → 渠道适配层问答 → 卡片回复。

        T19 身份：external_user_id 是飞书事件里的 sender open_id（没有则 None）。
        经 channels.resolve_actor 映射成渠道 Actor——**同一句提问由不同的人发出
        会拿到不同结果**（有权的人查得到、无权的人被静默拒答），库级权限与登录态
        问答口径一致；未映射的人走默认策略（只看公开库）。归因行因此记的也是
        "谁问的"（已映射=用户名，未映射=feishu:<open_id>）。
        """
        cfg_bot = feishu_bot.get_settings(sf)
        kb_id = cfg_bot["kb_id"]
        actor = channels.resolve_actor(sf, channel=CHANNEL,
                                       external_user_id=external_user_id)
        try:
            answer, citations = await channels.answer_for_channel(
                svc, kb_id=kb_id, question=question, actor=actor,
                provider=cfg_bot["provider"] or None)
            # T12 归因行由 answer_for_channel 统一落（channel=feishu）：
            # 越权也落（bucket=scope_denied），不在这里重复记一笔。
            answer = answer.strip() or "（未能生成回答）"
            app_id, app_secret = feishu.get_credentials(sf)
            token = feishu._get_token(app_id, app_secret)
            feishu_bot.reply_card(
                token, message_id,
                feishu_bot.build_answer_card(answer, citations))
            # 审计 actor 保持 "feishu-bot"（机器人是执行方）；提问者身份在归因行
            # 的 actor 里，两处不是一回事，不合并。
            write_audit(sf, actor="feishu-bot", action="feishu_bot_answer",
                        resource=f"kb_id={kb_id}", detail=question[:100])
        except Exception:  # noqa: BLE001 —— IM 场景吞错记日志，不能让飞书重推风暴
            logger.exception("飞书机器人回答失败: %s", question[:50])

    # ---- 公开组：事件回调（飞书开放平台"事件订阅"指向这里） ----

    @app.post("/api/feishu/events")
    async def feishu_events(request: Request, bg: BackgroundTasks):
        cfg_bot = feishu_bot.get_settings(sf)
        raw = await request.body()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPException(400, "事件体不是合法 JSON") from e

        # 加密模式：先验签（防伪造）再解密
        if "encrypt" in payload:
            encrypt_key = cfg_bot["encrypt_key"]
            if not encrypt_key:
                raise HTTPException(400, "收到加密事件但未配置 Encrypt Key")
            sig = request.headers.get("X-Lark-Signature", "")
            ts = request.headers.get("X-Lark-Request-Timestamp", "")
            nonce = request.headers.get("X-Lark-Request-Nonce", "")
            # url_verification 握手不带签名头；带头必须验过
            if sig and not feishu_bot.verify_signature(
                    encrypt_key, ts, nonce, raw, sig):
                raise HTTPException(403, "签名校验失败")
            try:
                payload = feishu_bot.decrypt_event(encrypt_key, payload["encrypt"])
            except Exception as e:  # noqa: BLE001
                raise HTTPException(400, "事件解密失败，请核对 Encrypt Key") from e

        # 握手：url_verification 无条件回 challenge——飞书"配置回调地址"时发，
        # 此刻 KBase 可能还没配飞书机器人（先验证地址、后填 token 是常见顺序，
        # 之前在此之前就 403"未配置"会让验证永远过不去）。配了 token 才校验来源，
        # 没配则直接回：明文握手回 challenge 无副作用，安全由后续真实事件的
        # token/签名校验保证。
        if payload.get("type") == "url_verification":
            vt = cfg_bot["verification_token"]
            if vt and payload.get("token") != vt:
                raise HTTPException(403, "verification token 不匹配")
            return {"challenge": payload.get("challenge", "")}

        # 真实业务事件才要求已配置（未配置的机器人不处理消息）
        if not cfg_bot["verification_token"]:
            raise HTTPException(403, "飞书机器人未配置")
        header = payload.get("header") or {}
        if header.get("token") != cfg_bot["verification_token"]:
            raise HTTPException(403, "verification token 不匹配")
        if feishu_bot.is_duplicate_event(header.get("event_id", "")):
            return {}                      # 飞书重推：已受理过，直接确认
        if header.get("event_type") != "im.message.receive_v1":
            return {}                      # 其他事件类型：确认但不处理

        parsed = feishu_bot.extract_question(payload)
        if parsed is None:
            return {}                      # 非文本/空消息：群里不是每条都是提问
        if not cfg_bot["kb_id"]:
            return {}                      # 未绑定库：静默确认（管理页会提示）
        question, message_id = parsed
        # T19：提问者的外部身份（sender open_id）随任务带下去。取不到 sender 的
        # 事件（少数形态）传 None，由 resolve_actor 走未映射默认策略。
        bg.add_task(_answer_and_reply, question, message_id,
                    feishu_bot.extract_sender_id(payload))
        return {}
