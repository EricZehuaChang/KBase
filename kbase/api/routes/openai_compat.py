"""OpenAI 兼容 API（M6-5）：把每个知识库暴露成一个"模型"，让任意 OpenAI
生态客户端（NextChat/LobeChat/各类 SDK）零改造接入企业知识库问答。

- GET  /v1/models            —— 可见知识库列表（ACL 过滤后），id=kb_id；
- POST /v1/chat/completions  —— model 填 kb_id（或唯一的库名），走与
  /api/kb/{id}/query 相同的检索+生成编排；stream=true 时输出
  chat.completion.chunk SSE（以 data: [DONE] 结尾），非流式返回完整
  chat.completion 对象。两种响应都附带 KBase 扩展字段 citations（引用溯源）。

鉴权与 /api 一致（Bearer API Key 或会话 Cookie），无权/不存在的库统一按
OpenAI 错误格式返回 404 model_not_found（不泄漏"存在但无权"）。
"""
import json
import time
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from kbase import kb_acl
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


def register(app, svc: Services, actor_dependency) -> None:
    sf, cfg, retriever = svc.sf, svc.cfg, svc.retriever
    router = APIRouter(prefix="/v1", dependencies=[Depends(actor_dependency)])

    def _resolve_kb(model: str, actor: dict) -> str | None:
        """model → kb_id：先按 id 精确匹配，再按库名匹配（仅当唯一时）。
        找不到或无权访问都返回 None（统一 404，不泄漏存在性）。"""
        with sf() as s:
            kb = s.get(KnowledgeBase, model)
            if kb is None:
                named = s.query(KnowledgeBase).filter(
                    KnowledgeBase.name == model).all()
                kb = named[0] if len(named) == 1 else None
            kb_id = kb.id if kb else None
        if kb_id is None or not kb_acl.can_access(sf, kb_id, actor):
            return None
        return kb_id

    @router.get("/models")
    def list_models(request: Request):
        """可见知识库=可用"模型"清单。owned_by 固定 kbase，客户端仅展示用。"""
        actor = getattr(request.state, "actor", None) or {"role": "admin"}
        mode, allowed = kb_acl.visible_kb_filter(sf, actor)
        with sf() as s:
            kbs = s.query(KnowledgeBase).order_by(KnowledgeBase.created_at).all()
            data = [{"id": kb.id, "object": "model",
                     "created": int(kb.created_at.timestamp()),
                     "owned_by": "kbase",
                     # KBase 扩展：库名，便于客户端下拉里人读
                     "display_name": kb.name}
                    for kb in kbs if mode == "all" or kb.id in allowed]
        return {"object": "list", "data": data}

    @router.post("/chat/completions")
    async def chat_completions(body: ChatCompletionsBody, request: Request):
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

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

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
                yield {"data": _chunk({"role": "assistant", "content": ""})}
                async for piece in gen.answer_stream(question, usable, history):
                    yield {"data": _chunk({"content": piece})}
                # 末块带引用（KBase 扩展字段，标准客户端忽略不影响兼容）
                yield {"data": _chunk({}, finish="stop",
                                      extra={"citations": citations})}
                yield {"data": "[DONE]"}
            return EventSourceResponse(events())

        pieces = [p async for p in gen.answer_stream(question, usable, history)]
        answer = "".join(pieces)
        return {"id": completion_id, "object": "chat.completion",
                "created": created, "model": body.model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": answer}}],
                # token 用量上游 provider 未透出，按 OpenAI 惯例给 0 值占位
                "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0},
                "citations": citations}

    app.include_router(router)
