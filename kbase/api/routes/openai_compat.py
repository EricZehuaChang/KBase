"""OpenAI 兼容 API（M6-5）：把每个知识库暴露成一个"模型"，让任意 OpenAI
生态客户端（NextChat/LobeChat/各类 SDK）零改造接入企业知识库问答。

- GET  /v1/models            —— 可见知识库列表（ACL 过滤后），id=kb_id；
- POST /v1/chat/completions  —— model 填 kb_id（或唯一的库名），走与
  /api/kb/{id}/query 相同的检索+生成编排；stream=true 时输出
  chat.completion.chunk SSE（以 data: [DONE] 结尾），非流式返回完整
  chat.completion 对象。两种响应都附带 KBase 扩展字段 citations（引用溯源）。

鉴权与 /api 一致（Bearer API Key 或会话 Cookie），无权/不存在的库统一按
OpenAI 错误格式返回 404 model_not_found（不泄漏"存在但无权"）。

T09：/v1 router 与 /api 一样挂限流依赖（kbase/ratelimit.py），生成的 token
用量按 Key 落库（api_key_usage_daily）。usage **优先取上游真实值**（provider
流式带 stream_options.include_usage，末块回传 usage）；只有端点不认该参数或
末块没带 usage 时才回退字符估算，并在响应里如实标注（非流式：
x-kbase-usage-estimated 响应头 + usage_estimated 字段；流式响应头来不及改判，
以末块 usage_estimated 字段为准）。
"""
import json
import time
import uuid

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from kbase import kb_acl
from kbase import qa_outcomes
from kbase import ratelimit
from kbase import retrieval_strategy as rs
from kbase.api.schemas import ChatCompletionsBody
from kbase.api.services import Services
from kbase.audit import write_query_audit
from kbase.models import KnowledgeBase
from kbase.rag.generator import Generator


def _error(status: int, message: str, code: str) -> JSONResponse:
    """OpenAI 风格错误体：客户端 SDK 按 error.code/error.message 解析。"""
    return JSONResponse(status_code=status, content={
        "error": {"message": message, "type": "invalid_request_error",
                  "code": code}})


def _extract_text(content) -> str:
    """OpenAI content 允许 str 或分段数组（[{type:"text",text:...},...]），
    统一抽成纯文本；非文本段（如图片）忽略。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and p.get("type") == "text")
    return ""


def register(app, svc: Services, actor_dependency, rate_limit_dependency) -> None:
    sf, cfg, retriever = svc.sf, svc.cfg, svc.retriever
    # 依赖顺序：actor 在前（写 request.state.actor），限流在后（读它判定配额）
    # ——与 /api router 同构，见 kbase/api/main.py。
    router = APIRouter(prefix="/v1", dependencies=[
        Depends(actor_dependency), Depends(rate_limit_dependency)])

    def _resolve_kb(model: str, actor: dict) -> str | None:
        """model → kb_id：先按 id 精确匹配，再按库名匹配（仅当唯一时）。
        找不到、无权访问、或超出 API Key 库级 scope 都返回 None
        （统一 404，不泄漏存在性）。

        T01/G01：ACL 与 scope 是两条独立闸门，必须都过——受限 key 可能是
        admin 角色（can_access 对 admin 直接豁免），只判 ACL 会让它访问
        白名单外的库。scope 判定不看角色，见 kb_acl.scope_allows。"""
        with sf() as s:
            kb = s.get(KnowledgeBase, model)
            if kb is None:
                named = s.query(KnowledgeBase).filter(
                    KnowledgeBase.name == model).all()
                kb = named[0] if len(named) == 1 else None
            kb_id = kb.id if kb else None
        if kb_id is None or not kb_acl.can_access(sf, kb_id, actor):
            return None
        if not kb_acl.scope_allows(actor, kb_id):
            return None
        return kb_id

    @router.get("/models")
    def list_models(request: Request):
        """可见知识库=可用"模型"清单。owned_by 固定 kbase，客户端仅展示用。

        T01/G01：ACL 过滤后再叠加 API Key 库级 scope——受限 key 不得列出
        白名单外的库（否则等于把不可访问的库名暴露给集成方）。"""
        actor = getattr(request.state, "actor", None) or {"role": "admin"}
        mode, allowed = kb_acl.visible_kb_filter(sf, actor)
        with sf() as s:
            kbs = s.query(KnowledgeBase).order_by(KnowledgeBase.created_at).all()
            listed = [kb for kb in kbs if mode == "all" or kb.id in allowed]
            in_scope = kb_acl.apply_scope(actor, [kb.id for kb in listed])
            data = [{"id": kb.id, "object": "model",
                     "created": int(kb.created_at.timestamp()),
                     "owned_by": "kbase",
                     # KBase 扩展：库名，便于客户端下拉里人读
                     "display_name": kb.name}
                    for kb in listed if kb.id in in_scope]
        return {"object": "list", "data": data}

    @router.post("/chat/completions")
    async def chat_completions(body: ChatCompletionsBody, request: Request,
                              response: Response):
        actor = getattr(request.state, "actor", None) or {"role": "admin"}
        kb_id = _resolve_kb(body.model, actor)
        if kb_id is None:
            return _error(404, f"The model '{body.model}' does not exist.",
                          "model_not_found")

        # 取最后一条 user 消息为问题，之前的 user/assistant 轮次为对话历史；
        # 客户端自带的 system 消息丢弃——生成器有自己的知识库问答 system prompt，
        # 外部 system 注入会破坏"只依据资料回答"的约束。
        question = ""
        history: list[dict] = []
        for m in body.messages:
            text = _extract_text(m.content)
            if m.role == "user":
                if question:
                    history.append({"role": "user", "content": question})
                question = text
            elif m.role == "assistant":
                history.append({"role": "assistant", "content": text})
        if not question.strip():
            return _error(400, "No user message found in 'messages'.",
                          "invalid_messages")

        try:
            llm = svc.get_llm(None)        # 站点当前激活的生成模型
        except (KeyError, RuntimeError) as e:
            return _error(503, str(e), "provider_unavailable")

        write_query_audit(sf, request, resource=f"kb_id={kb_id}",
                          question=question)

        # 与 /api/kb/{id}/query 同一份检索+生成语义（KB 策略、拒答阈值量纲）。
        strategy = rs.resolve_strategy(cfg, rs.kb_retrieval_config(sf, kb_id))
        min_score = rs.pick_min_score(cfg, strategy, retriever.rerank_active)
        blocks = await run_in_threadpool(
            retriever.retrieve, kb_id, question, body.top_k, False, strategy)
        gen = Generator(llm, min_score=min_score,
                        min_include_score=cfg.retrieval.min_include_score)
        usable = gen.usable_blocks(blocks)
        citations = gen.citations(usable)

        # 拒答同样落 query_refused 审计（与 /api 问答口径一致，喂运营看板）。
        if not usable:
            from kbase.audit import write_audit
            client = request.client
            write_audit(sf, actor=(actor.get("name") or "unknown"),
                        action="query_refused", resource=f"kb_id={kb_id}",
                        detail=question[:100],
                        ip=(client.host if client else None))

        # T12 归因：/v1 是独立编排（不走 routes/query.py 的 _run_query），
        # 归因行在这里单独落一行，渠道 v1。actor 取 Key/会话身份名——「集成方
        # 用哪个 Key 问不出来」与「终端用户在页面上问不出来」是两件事。
        qa_outcomes.record_query_outcome(
            sf, channel="v1", kb_id=kb_id, question=question, blocks=blocks,
            usable=usable, actor=actor.get("name"))

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        # ---- T09 用量计量 ----
        # **真实用量优先**：调用 provider 时带 usage_sink，拿到上游真实
        # usage（OpenAI 兼容端点经 stream_options.include_usage 在末块透出，
        # 2026-09-14 于 DashScope 实测 prompt=14/completion=1 与逐字一致）
        # 就直接记真实值；只有端点不接受该参数（provider 内部已降级重试，
        # 见 kbase/plugins/llm/openai_compat.py）或本次根本没调 LLM 时，
        # 才退回字符估算并置 tokens_estimated / 加 x-kbase-usage-estimated。
        key_id = actor.get("key_id")
        prompt_text = "\n".join([question, *[b.text for b in usable],
                                 *[m["content"] for m in history]])

        def _usage(answer: str, real: dict | None = None) -> tuple[dict, bool]:
            """(usage 对象, 是否估算)。优先级：上游真实值 > 字符估算。
            无可用依据=拒答短路、根本没调 LLM，token 记 0 且**不算估算**
            ——0 是精确值，不是估出来的。"""
            if not usable:
                return {"prompt_tokens": 0, "completion_tokens": 0,
                        "total_tokens": 0}, False
            if real:
                return dict(real), False
            prompt_tokens = ratelimit.estimate_tokens(prompt_text)
            completion_tokens = ratelimit.estimate_tokens(answer)
            return {"prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens}, True

        def _meter(prompt_tokens: int, completion_tokens: int,
                   estimated: bool) -> None:
            """按 Key 记用量；会话 Cookie 身份没有 key_id → 不记（用量是
            per-key 概念）。写入失败不应影响回答本身。"""
            if not key_id or not (prompt_tokens or completion_tokens):
                return
            try:
                ratelimit.record_tokens(sf, key_id, prompt_tokens=prompt_tokens,
                                        completion_tokens=completion_tokens,
                                        tokens_estimated=estimated)
            except Exception:      # noqa: BLE001 计量是旁路，绝不拖垮问答
                import logging
                logging.getLogger(__name__).exception("API Key 用量落库失败")

        def _chunk(delta: dict, finish: str | None = None,
                   extra: dict | None = None) -> str:
            payload = {"id": completion_id, "object": "chat.completion.chunk",
                       "created": created, "model": body.model,
                       "choices": [{"index": 0, "delta": delta,
                                    "finish_reason": finish}]}
            if extra:
                payload.update(extra)
            return json.dumps(payload, ensure_ascii=False)

        if body.stream:
            async def events():
                pieces: list[str] = []
                # provider 拿到真实 usage 就写进这里（usage_sink 回调）
                real_usage: dict = {}
                usage = {"prompt_tokens": 0, "completion_tokens": 0,
                         "total_tokens": 0}
                try:
                    yield {"data": _chunk({"role": "assistant", "content": ""})}
                    async for piece in gen.answer_stream(
                            question, usable, history, usage_sink=real_usage.update):
                        pieces.append(piece)
                        yield {"data": _chunk({"content": piece})}
                finally:
                    # 客户端中断也走这里：已生成的部分答案照实计量（与
                    # query.py 会话落库的 finally 同一考虑）。真实 usage 只在
                    # 上游把末块发完时才拿得到，中断时自然退回估算。
                    usage, estimated = _usage("".join(pieces), real_usage or None)
                    _meter(usage["prompt_tokens"], usage["completion_tokens"],
                           estimated)
                # 末块带引用与用量（KBase 扩展字段，标准客户端忽略不影响兼容；
                # OpenAI 的流式 usage 也出现在末块，位置一致）。
                # usage_estimated：本响应的口径标记，**流式下这是权威来源**——
                # SSE 响应头在首个 chunk 之前就已发出，那时还不知道上游会不会
                # 回传 usage（端点不认 stream_options 时 provider 会内部降级），
                # 事后无法改判响应头，所以流式不发 x-kbase-usage-estimated，
                # 改由这里如实标注（true=按字符数估算，false=上游真实值）。
                yield {"data": _chunk({}, finish="stop",
                                      extra={"citations": citations,
                                             "usage": usage,
                                             "usage_estimated": estimated})}
                yield {"data": "[DONE]"}
            # 事件序列（chunk*→末块→[DONE]）与引入计量前完全一致。
            return EventSourceResponse(events())

        real_usage: dict = {}
        pieces = [p async for p in gen.answer_stream(
            question, usable, history, usage_sink=real_usage.update)]
        answer = "".join(pieces)
        usage, estimated = _usage(answer, real_usage or None)
        _meter(usage["prompt_tokens"], usage["completion_tokens"], estimated)
        # 非流式在响应发出前就已知口径，除 usage_estimated 字段外再给一个
        # 响应头（流式给不了，理由见上）。
        if estimated:
            response.headers["x-kbase-usage-estimated"] = "1"
        return {"id": completion_id, "object": "chat.completion",
                "created": created, "model": body.model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": answer}}],
                # token 用量：上游真实值优先，拿不到才按字符数估算
                # （估算时带 x-kbase-usage-estimated 头标注，见上）
                "usage": usage,
                "usage_estimated": estimated,
                "citations": citations}

    app.include_router(router)
