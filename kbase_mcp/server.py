"""KBase MCP Server：把知识库暴露为 MCP 工具。
通过 HTTP 反调运行中的 KBase API（默认 http://localhost:8100），
不直接加载内核——避免模型双份驻留。"""
import json
import os
from dataclasses import dataclass

import httpx
from mcp.server.fastmcp import FastMCP

DEFAULT_API = os.environ.get("KBASE_API_URL", "http://localhost:8100")
_UNREACHABLE = ("KBase 服务不可达（{url}）。请先启动：uvicorn --factory "
                "kbase.api.main:create_app --port 8100")
_NEEDS_API_KEY = ("KBase 服务已开启鉴权，但未配置 API Key。请设置环境变量 "
                  "KBASE_API_KEY（在 KBase 设置页的「API Key」卡片创建一个，"
                  "角色按需选择 viewer/editor/admin），再重启本 MCP Server。")


@dataclass
class KBaseClient:
    http: httpx.AsyncClient


def build_default_client() -> "KBaseClient":
    """构造未显式注入 client 时使用的默认 KBaseClient：base_url 取
    KBASE_API_URL（同既有逻辑）；若 env KBASE_API_KEY 已设置，则给
    httpx.AsyncClient 挂上默认 Authorization: Bearer 头，之后该 client
    发出的每一次反调请求都自动携带鉴权，不需要调用方在每次请求时手工传。
    未设置 KBASE_API_KEY 时不加该头——对应 auth="off" 的部署或本机可信环境，
    行为与鉴权改造前一致。"""
    api_key = os.environ.get("KBASE_API_KEY")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    return KBaseClient(httpx.AsyncClient(base_url=DEFAULT_API, headers=headers))


def _err(url: str) -> dict:
    return {"error": _UNREACHABLE.format(url=url)}


def _wrap_status_error(e: httpx.HTTPStatusError, body: str) -> dict:
    """401（未认证：缺 Cookie/Bearer 或 API Key 已吊销）时不透传裸的 401
    错误体——那对着 MCP 客户端的使用者（通常不了解 KBase 内部鉴权机制）没有
    可操作性；改成清晰指引配置 KBASE_API_KEY 的中文提示。其余状态码
    （403 权限不足/404/5xx 等）原样透传响应体，那些错误本身已经可读。"""
    if e.response.status_code == 401:
        return {"error": _NEEDS_API_KEY}
    return {"error": body}


async def list_knowledge_bases_impl(c: KBaseClient):
    try:
        r = await c.http.get("/api/kb")
        r.raise_for_status()
    except httpx.TransportError:
        return _err(str(c.http.base_url))
    except httpx.HTTPStatusError as e:
        return _wrap_status_error(e, e.response.text)
    return [{"id": k["id"], "name": k["name"]} for k in r.json()]


async def search_knowledge_impl(c: KBaseClient, kb_id: str, query: str,
                                top_k: int = 5,
                                filters: dict | None = None):
    body: dict = {"query": query, "top_k": top_k}
    if filters:
        body["filters"] = filters
    try:
        r = await c.http.post(f"/api/kb/{kb_id}/search",
                              json=body,
                              timeout=120)
        r.raise_for_status()
    except httpx.TransportError:
        return _err(str(c.http.base_url))
    except httpx.HTTPStatusError as e:
        return _wrap_status_error(e, e.response.text)
    return [{"doc_name": b["doc_name"], "heading_path": b["heading_path"],
             "text": b["text"], "score": b["score"]}
            for b in r.json()["blocks"]]


async def ask_knowledge_base_impl(c: KBaseClient, kb_id: str, question: str,
                                  provider: str | None = None,
                                  filters: dict | None = None):
    """SSE 组装：比照 web-app/src/lib/sse.ts 的 accumulate-flush 逻辑移植。
    sse-starlette 对含 \\n 的 token 事件会拆成多条 data 行（SSE 规范），
    因此必须按事件收集 dataLines，事件边界（空行）处以 "\\n" join 后再 flush，
    而不能简单地把所有 token 事件的 data 用 "" 拼接——那样会丢失 token 内部换行。
    """
    body = {"question": question}
    if provider:
        body["provider"] = provider
    if filters:
        body["filters"] = filters
    answer_parts, citations = [], []
    event = ""
    data_lines: list[str] = []

    def flush():
        nonlocal event, data_lines
        if not event and not data_lines:
            return
        text = "\n".join(data_lines)
        if event == "token":
            answer_parts.append(text)
        elif event == "citations" and text:
            citations.extend(json.loads(text))
        event, data_lines = "", []

    try:
        async with c.http.stream("POST", f"/api/kb/{kb_id}/query",
                                 json=body, timeout=300) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
                elif line == "":
                    flush()
            flush()
    except httpx.TransportError:
        return _err(str(c.http.base_url))
    except httpx.HTTPStatusError as e:
        body = (await e.response.aread()).decode("utf-8", "replace")
        return _wrap_status_error(e, body)
    return {"answer": "".join(answer_parts),
            "citations": [{"doc_name": ci["doc_name"],
                           "heading_path": ci["heading_path"],
                           "snippet": ci["snippet"]} for ci in citations]}


# T14：批次读取两个只读端点。越权（受限 API Key 的库级 scope）时服务端返回
# **静默空集**——`{}` / `[]`，与 /search、/query 的越权语义一致；这里原样回空，
# 不编造字段也不改成错误面（"块不存在"在服务端已是 404，另走下面的错误面）。
async def get_chunk_impl(c: KBaseClient, chunk_id: str):
    try:
        r = await c.http.get(f"/api/chunks/{chunk_id}")
        r.raise_for_status()
    except httpx.TransportError:
        return _err(str(c.http.base_url))
    except httpx.HTTPStatusError as e:
        return _wrap_status_error(e, e.response.text)
    data = r.json()
    if not data:
        return {}
    return {"doc_id": data["doc_id"], "doc_name": data["doc_name"],
            "heading_path": data["heading_path"], "text": data["text"],
            "page": data["page"], "layout": data["layout"]}


async def get_document_outline_impl(c: KBaseClient, doc_id: str):
    try:
        r = await c.http.get(f"/api/documents/{doc_id}/outline")
        r.raise_for_status()
    except httpx.TransportError:
        return _err(str(c.http.base_url))
    except httpx.HTTPStatusError as e:
        return _wrap_status_error(e, e.response.text)
    return r.json()


async def submit_standard_answer_impl(c: KBaseClient, kb_id: str, question: str,
                                      answer: str,
                                      similar_questions: list[str] | None = None,
                                      category: str | None = None):
    """T14 → T13 建标问端点（POST /api/kb/{kb_id}/standard-answers）。

    **只提交、不生效**：服务端把新建标问一律置 pending_review（请求体里根本没有
    status 字段，创建者不能自录自过），审核通过前它不进索引、不参与召回——
    这正是标问库的红线（严禁"相似度命中就绕过检索直接返回答案"）。
    响应体原样透传（服务端返回创建后的标问记录，含 id / status）。"""
    # source=mcp：与人工录入（manual）区分，运营审核时能看出这是 Agent 提的
    body: dict = {"question": question, "answer": answer, "source": "mcp"}
    if similar_questions:
        body["similar_questions"] = similar_questions
    if category:
        body["category"] = category
    try:
        r = await c.http.post(f"/api/kb/{kb_id}/standard-answers", json=body)
        r.raise_for_status()
    except httpx.TransportError:
        return _err(str(c.http.base_url))
    except httpx.HTTPStatusError as e:
        return _wrap_status_error(e, e.response.text)
    return r.json()


# filters 参数说明（写进工具 description，agent 才会正确使用）。部署方可用
# KBASE_MCP_FILTERS_DOC 追加受控词表（如 ztenith 方案卡的行业/数据实体枚举）
# ——词表随 cards 仓库演进，不硬编码进 kbase 通用发行版。
_FILTERS_DOC = (
    "filters（可选）：按 chunk 元数据过滤，形如 {\"industry\": \"零售\"} 或 "
    "{\"data_entities\": [\"订单\", \"库存\"]}；字段间 AND，列表值内 OR。"
    "数值范围写成 {\"功率\": {\"gte\": 450, \"lte\": 550}}（上下界可只给一个），"
    "或用相对公差 {\"功率\": {\"approx\": 500, \"tol\": 0.1}}（±10%）；"
    "范围条件命中参数区间与查询区间有交集的表格块。"
    "仅对带 front matter 元数据摄取的文档（如方案卡）生效。")


def _filters_doc() -> str:
    extra = os.environ.get("KBASE_MCP_FILTERS_DOC", "").strip()
    return _FILTERS_DOC + ("\n" + extra if extra else "")


def build_mcp(client: KBaseClient | None = None) -> FastMCP:
    mcp = FastMCP("kbase")
    # 默认 client 有意随进程存活（不 aclose）：MCP Server 生命周期＝进程生命周期，
    # 连接与套接字在进程退出时由操作系统统一回收；测试注入的 client 由测试自管。
    c = client or build_default_client()

    @mcp.tool()
    async def list_knowledge_bases() -> list | dict:
        """列出全部知识库（id 与名称）。"""
        return await list_knowledge_bases_impl(c)

    @mcp.tool(description=(
        "在指定知识库中检索，返回带出处与相关度的原文块（不生成答案）。\n"
        + _filters_doc()))
    async def search_knowledge(kb_id: str, query: str, top_k: int = 5,
                               filters: dict | None = None) -> list | dict:
        return await search_knowledge_impl(c, kb_id, query, top_k, filters)

    @mcp.tool(description=(
        "对指定知识库完整 RAG 问答，返回答案与引用。\n" + _filters_doc()))
    async def ask_knowledge_base(kb_id: str, question: str,
                                 provider: str | None = None,
                                 filters: dict | None = None) -> dict | list:
        # 返回标注写成 `dict | list`（而非直觉的裸 `dict`）：FastMCP 的
        # func_metadata 对裸 `dict` 返回值不生成 output_schema（落入
        # "其他类class" 分支、get_type_hints(dict) 为空，模型创建失败），
        # 导致 CallToolResult.structuredContent 恒为 None；只要标注是
        # list/dict 的 Union（这里从不会真的返回 list，仅借用触发条件），
        # SDK 就会把结果包进 {"result": ...} 并生成 schema，
        # 与另外两个工具的 structuredContent["result"] 形状保持一致。
        return await ask_knowledge_base_impl(c, kb_id, question, provider,
                                             filters)

    # 下面三个工具的返回标注同样写成 list/dict 的 Union（理由见上方 ask 的注释）：
    # 裸 `dict` 不生成 output_schema，structuredContent 恒为 None。

    @mcp.tool(description=(
        "按 chunk_id 读取单个分块，返回正文与出处："
        "{doc_id, doc_name, heading_path, text, page, layout}"
        "（layout 为表格块的版式 JSON，非表格块为 null）。\n"
        "chunk_id 可从检索结果的引用或文档分块列表中拿到；"
        "越权或不可见时返回空对象 {}。"))
    async def get_chunk(chunk_id: str) -> dict | list:
        return await get_chunk_impl(c, chunk_id)

    @mcp.tool(description=(
        "读取一份文档的章节大纲：按 heading_path 去重后的父块树，"
        "返回 [{title, heading_path, children}]（按原文出现顺序，children 可嵌套）。\n"
        "用来先看文档有哪些章节再决定检索范围；越权或不可见时返回空数组 []。"))
    async def get_document_outline(doc_id: str) -> list | dict:
        return await get_document_outline_impl(c, doc_id)

    @mcp.tool(description=(
        "向知识库提交一条标准问答（question + answer，可选 similar_questions 相似问法、"
        "category 分类）。提交**只进入人工审核队列**：审核通过前既不进索引也不参与"
        "检索与问答，不会改变任何答案。\n"
        "返回创建后的标问记录（含 id 与 status=pending_review）；"
        "需 editor 及以上角色的 API Key，权限不足返回 403 错误对象。"))
    async def submit_standard_answer(kb_id: str, question: str, answer: str,
                                     similar_questions: list[str] | None = None,
                                     category: str | None = None) -> dict | list:
        return await submit_standard_answer_impl(
            c, kb_id, question, answer, similar_questions, category)

    return mcp
