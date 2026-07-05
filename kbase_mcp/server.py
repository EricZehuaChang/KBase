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
                                top_k: int = 5):
    try:
        r = await c.http.post(f"/api/kb/{kb_id}/search",
                              json={"query": query, "top_k": top_k},
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
                                  provider: str | None = None):
    """SSE 组装：比照 web-app/src/lib/sse.ts 的 accumulate-flush 逻辑移植。
    sse-starlette 对含 \\n 的 token 事件会拆成多条 data 行（SSE 规范），
    因此必须按事件收集 dataLines，事件边界（空行）处以 "\\n" join 后再 flush，
    而不能简单地把所有 token 事件的 data 用 "" 拼接——那样会丢失 token 内部换行。
    """
    body = {"question": question}
    if provider:
        body["provider"] = provider
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


def build_mcp(client: KBaseClient | None = None) -> FastMCP:
    mcp = FastMCP("kbase")
    # 默认 client 有意随进程存活（不 aclose）：MCP Server 生命周期＝进程生命周期，
    # 连接与套接字在进程退出时由操作系统统一回收；测试注入的 client 由测试自管。
    c = client or build_default_client()

    @mcp.tool()
    async def list_knowledge_bases() -> list | dict:
        """列出全部知识库（id 与名称）。"""
        return await list_knowledge_bases_impl(c)

    @mcp.tool()
    async def search_knowledge(kb_id: str, query: str, top_k: int = 5) -> list | dict:
        """在指定知识库中检索，返回带出处与相关度的原文块（不生成答案）。"""
        return await search_knowledge_impl(c, kb_id, query, top_k)

    @mcp.tool()
    async def ask_knowledge_base(kb_id: str, question: str,
                                 provider: str | None = None) -> dict | list:
        """对指定知识库完整 RAG 问答，返回答案与引用。
        返回标注写成 `dict | list`（而非直觉的裸 `dict`）：FastMCP 的
        func_metadata 对裸 `dict` 返回值不生成 output_schema（落入
        "其他类class" 分支、get_type_hints(dict) 为空，模型创建失败），
        导致 CallToolResult.structuredContent 恒为 None；只要标注是
        list/dict 的 Union（这里从不会真的返回 list，仅借用触发条件），
        SDK 就会把结果包进 {"result": ...} 并生成 schema，
        与另外两个工具的 structuredContent["result"] 形状保持一致。"""
        return await ask_knowledge_base_impl(c, kb_id, question, provider)

    return mcp
