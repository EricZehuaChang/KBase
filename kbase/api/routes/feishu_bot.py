"""飞书群机器人路由（对标 #2）：管理配置（admin）+ 事件回调（公开）。

事件回调必须 3 秒内响应，检索+生成放 BackgroundTasks 异步执行、完成后
调飞书 reply 接口——与免登录分享同一"公开端点+后台重活"结构。
安全：加密模式验签+解密；明文模式核对 verification token；两者都不过=403。
"""
import json
import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from kbase import feishu, feishu_bot
from kbase import retrieval_strategy as rs
from kbase.api.routes import RouteDeps
from kbase.api.schemas import FeishuBotSettingsBody
from kbase.api.services import Services
from kbase.audit import write_audit
from kbase.models import KnowledgeBase
from kbase.rag.generator import Generator

logger = logging.getLogger(__name__)


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
                raise HTTPException(422, f"知识库不存在: {body.kb_id}")
        feishu_bot.set_settings(sf, verification_token=body.verification_token,
                                encrypt_key=body.encrypt_key,
                                kb_id=body.kb_id, provider=body.provider)
        return feishu_bot.status(sf)

    # ---- 后台：检索+生成+回复（非流式——IM 场景一次性出完整答案） ----

    async def _answer_and_reply(question: str, message_id: str) -> None:
        cfg_bot = feishu_bot.get_settings(sf)
        kb_id = cfg_bot["kb_id"]
        try:
            llm = svc.get_llm(cfg_bot["provider"] or None)
            strategy = rs.resolve_strategy(
                svc.cfg, rs.kb_retrieval_config(sf, kb_id))
            min_score = rs.pick_min_score(svc.cfg, strategy,
                                          svc.retriever.rerank_active)
            blocks = await run_in_threadpool(
                svc.retriever.retrieve, kb_id, question, 5, False, strategy)
            gen = Generator(llm, min_score=min_score,
                            min_include_score=svc.cfg.retrieval.min_include_score)
            usable = gen.usable_blocks(blocks)
            citations = gen.citations(usable)
            pieces = [p async for p in gen.answer_stream(question, usable, None)]
            answer = "".join(pieces).strip() or "（未能生成回答）"
            app_id, app_secret = feishu.get_credentials(sf)
            token = feishu._get_token(app_id, app_secret)
            feishu_bot.reply_card(
                token, message_id,
                feishu_bot.build_answer_card(answer, citations))
            write_audit(sf, actor="feishu-bot", action="feishu_bot_answer",
                        resource=f"kb_id={kb_id}", detail=question[:100])
        except Exception:  # noqa: BLE001 —— IM 场景吞错记日志，不能让飞书重推风暴
            logger.exception("飞书机器人回答失败: %s", question[:50])

    # ---- 公开组：事件回调（飞书开放平台"事件订阅"指向这里） ----

    @app.post("/api/feishu/events")
    async def feishu_events(request: Request, bg: BackgroundTasks):
        cfg_bot = feishu_bot.get_settings(sf)
        if not cfg_bot["verification_token"]:
            raise HTTPException(403, "飞书机器人未配置")
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

        # 握手：原样回 challenge（明文模式核对 token）
        if payload.get("type") == "url_verification":
            if payload.get("token") != cfg_bot["verification_token"]:
                raise HTTPException(403, "verification token 不匹配")
            return {"challenge": payload.get("challenge", "")}

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
        bg.add_task(_answer_and_reply, question, message_id)
        return {}
