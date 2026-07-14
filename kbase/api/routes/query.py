"""问答与会话域路由：直接问答、检索调试、会话 CRUD 与会话内多轮问答。

_run_query 是本域的共享编排核心：旧端点 /api/kb/{id}/query 与会话端点
/api/conversations/{id}/query 复用同一份检索+生成逻辑，保证事件序列
（citations→token*→done）与拒答语义完全一致。"""
import json
from dataclasses import asdict

from fastapi import HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from sse_starlette.sse import EventSourceResponse

from kbase import conversations as conv_store
from kbase import retrieval_strategy as rs
from kbase.api.routes import RouteDeps
from kbase.api.schemas import (ConversationCreate, ConversationRename,
                               QueryBody, SearchBody)
from kbase.api.services import Services
from kbase.audit import write_query_audit
from kbase.rag.generator import Generator


def register(router, svc: Services, deps: RouteDeps) -> None:
    sf, cfg, retriever = svc.sf, svc.cfg, svc.retriever

    async def _run_query(kb_id: str, body: QueryBody, *,
                         history: list[dict] | None = None,
                         on_complete=None, retrieval_query: str | None = None):
        """共享检索+生成编排：会话端点与旧的 /api/kb/{id}/query 端点复用同一份
        逻辑，保证事件序列（citations→token*→done）与拒答语义完全一致。

        on_complete(answer_text, citations, provider): 流结束（含客户端中断）
        后调用，用于会话落库；旧端点不传，行为与改造前完全相同。
        retrieval_query: 检索实际使用的问题文本；默认 None 时等同 body.question
        （旧端点 /api/kb/{id}/query 不传，行为字节级不变）。会话端点在触发
        QueryRewrite 时传入改写后的问题——生成（answer_stream）与落库
        （on_complete）仍固定使用 body.question（原文），只有检索这一步换词。
        """
        try:
            llm = svc.get_llm(body.provider)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:      # 环境变量未设置等初始化失败：给前端可读信息
            raise HTTPException(503, str(e)) from e
        # KB 级检索策略（M6-1.5）：缺省=全局默认；拒答阈值量纲跟着"本次是否
        # 重排"走（per-call），不再用启动期静态值。
        strategy = rs.resolve_strategy(cfg, rs.kb_retrieval_config(sf, kb_id))
        min_score = rs.pick_min_score(cfg, strategy, retriever.rerank_active)
        query_text = retrieval_query if retrieval_query is not None else body.question
        # 检索（含向量化）是同步 CPU/IO 混合操作，进线程池避免阻塞事件循环
        blocks = await run_in_threadpool(
            retriever.retrieve, kb_id, query_text, body.top_k, False, strategy)
        gen = Generator(llm, min_score=min_score,
                        min_include_score=cfg.retrieval.min_include_score)
        # 关键契约：usable_blocks 只算一次，citations 与 answer_stream 用同一份列表，
        # 保证引用编号与答案中的 [n] 标记对齐（拒答时 citations 为空列表）
        usable = gen.usable_blocks(blocks)
        citations = gen.citations(usable)

        async def events():
            pieces: list[str] = []
            try:
                yield {"event": "citations",
                       "data": json.dumps(citations, ensure_ascii=False)}
                async for piece in gen.answer_stream(body.question, usable, history):
                    pieces.append(piece)
                    yield {"event": "token", "data": piece}
                yield {"event": "done", "data": ""}
            finally:
                # 客户端中断（GeneratorExit）时也执行：已生成的部分答案（可能为空）
                # 连同引用一并落库，拒答场景（usable 为空）同样落库。
                if on_complete is not None:
                    on_complete("".join(pieces), citations,
                               getattr(llm, "model", body.provider or cfg.llm.active))

        return EventSourceResponse(events())

    @router.post("/kb/{kb_id}/query", dependencies=[deps.require_viewer])
    async def query(kb_id: str, body: QueryBody, request: Request):
        write_query_audit(sf, request, resource=f"kb_id={kb_id}", question=body.question)
        return await _run_query(kb_id, body)

    @router.post("/kb/{kb_id}/search", dependencies=[deps.require_viewer])
    async def search(kb_id: str, body: SearchBody):
        """检索调试端点：debug=False 只返回 blocks（不含 trace key，向后兼容展示用途）；
        debug=True 额外返回各阶段 trace（dense/keyword/fused[/reranked]），用于排查召回质量。
        body 的 use_keyword/use_rerank/candidates 为请求级策略覆盖（试跑对比用，
        不落库；缺省=KB 策略/全局默认）。检索进线程池避免阻塞事件循环。"""
        strategy = rs.resolve_strategy(
            cfg, rs.kb_retrieval_config(sf, kb_id),
            overrides={"use_keyword": body.use_keyword,
                       "use_rerank": body.use_rerank,
                       "candidates": body.candidates})
        result = await run_in_threadpool(
            retriever.retrieve, kb_id, body.query, body.top_k, body.debug,
            strategy)
        if body.debug:
            return {"blocks": [asdict(b) for b in result.blocks],
                    "trace": result.trace}
        return {"blocks": [asdict(b) for b in result]}

    # 会话创建/查看要求 require_viewer 而不是 require_editor：spec（M4-1 §3
    # 角色矩阵）"问答/会话/检索/生成任务查看"一整行都是 viewer 可过——使用端
    # （M5-1 F2）的主力用户就是 viewer，如果连"新建会话"都要 editor 权限，
    # viewer 就完全没法用问答页，这是与设计文档矛盾的实现疏漏，这里一并修正。
    @router.post("/conversations", dependencies=[deps.require_viewer, deps.audit_mutation])
    def create_conversation(body: ConversationCreate, request: Request):
        actor = request.state.actor
        return conv_store.create_conversation(sf, body.kb_id, user_id=actor.get("user_id"))

    @router.get("/conversations", dependencies=[deps.require_viewer])
    def list_conversations(request: Request, kb_id: str | None = None,
                           limit: int = Query(default=30, ge=1, le=100),
                           offset: int = Query(default=0, ge=0)):
        actor = request.state.actor
        return conv_store.list_conversations(sf, kb_id, limit=limit, offset=offset,
                                             user_id=actor.get("user_id"))

    @router.get("/conversations/{conv_id}/messages", dependencies=[deps.require_viewer])
    def list_conversation_messages(conv_id: str, request: Request):
        actor = request.state.actor
        # 归属校验先行：不可见（不存在/不是你的）统一 404，不把"存在但无权"
        # 泄漏成 403（403 会暴露 conv_id 确实存在，见 conv_store.get_conversation）。
        if conv_store.get_conversation(sf, conv_id, user_id=actor.get("user_id")) is None:
            raise HTTPException(404, f"会话不存在: {conv_id}")
        return conv_store.list_messages(sf, conv_id)

    @router.put("/conversations/{conv_id}", dependencies=[deps.require_viewer, deps.audit_mutation])
    def rename_conversation(conv_id: str, body: ConversationRename, request: Request):
        actor = request.state.actor
        title = body.title.strip()
        if not title:
            raise HTTPException(422, "会话标题不能为空")
        result = conv_store.rename_conversation(sf, conv_id, title,
                                                user_id=actor.get("user_id"))
        if result is None:
            raise HTTPException(404, f"会话不存在: {conv_id}")
        return result

    @router.delete("/conversations/{conv_id}",
                   dependencies=[deps.require_viewer, deps.audit_mutation])
    def delete_conversation(conv_id: str, request: Request):
        actor = request.state.actor
        ok = conv_store.delete_conversation(sf, conv_id, user_id=actor.get("user_id"))
        if not ok:
            raise HTTPException(404, f"会话不存在: {conv_id}")
        return {"ok": True}

    @router.post("/conversations/{conv_id}/query", dependencies=[deps.require_viewer])
    async def query_conversation(conv_id: str, body: QueryBody, request: Request):
        actor = request.state.actor
        conv = conv_store.get_conversation(sf, conv_id, user_id=actor.get("user_id"))
        if conv is None:
            raise HTTPException(404, f"会话不存在: {conv_id}")
        write_query_audit(sf, request, resource=f"conv_id={conv_id}", question=body.question)
        history = conv_store.build_history(sf, conv_id)
        # 改写只影响检索输入；生成与落库仍固定使用 body.question（原文），
        # 见 _run_query 的 retrieval_query 参数文档。
        # M6-1.5：改写模式按 KB 策略覆盖——off 时连 LLM 都不碰（省一次调用），
        # 其余模式传给 rewriter 做 should_rewrite 判定。
        strategy = rs.resolve_strategy(cfg, rs.kb_retrieval_config(sf, conv.kb_id))
        if strategy.rewrite_mode == "off":
            from kbase.rag.rewriter import RewriteResult
            rewrite_res = RewriteResult(query=body.question, triggered=False,
                                        rewritten=False)
        else:
            rewrite_res = await svc.rewriter.rewrite(body.question, history,
                                                     mode=strategy.rewrite_mode)

        def _persist(answer: str, citations: list[dict], provider: str):
            conv_store.append_round(sf, conv_id, body.question, answer,
                                    citations, provider)

        return await _run_query(conv.kb_id, body, history=history,
                               retrieval_query=rewrite_res.query,
                               on_complete=_persist)
