# KBase M3-2 设计文档 — MCP Server（对外 Agent 协作）

- 日期：2026-07-05
- 状态：授权自主执行（M3 总授权内）
- 前置：M3-1 已合并（107 backend / 34 frontend 全绿）

## 1. 目标与定位

把知识库暴露为标准 MCP 工具，让外部 Agent（Claude Code/Desktop、客户自建 Agent）能检索与问答——这是产品"Agent 协作·对外"的商业卖点（M2 spec §1 既定）。对齐 markitdown-mcp 的形态：独立进程、STDIO + Streamable HTTP 双传输。

## 2. 架构决策

**MCP 进程通过 HTTP 调用运行中的 KBase API**，不直接 import 内核组件。理由：避免 bge/reranker 模型在两个进程重复加载（~4GB 内存），天然与 API 层的鉴权/审计路径一致，MCP 进程轻到毫秒级启动。代价是要求 KBase 服务在跑——文档写明，工具报错信息清晰指引。

```
外部 Agent ⇄ (STDIO / Streamable HTTP) ⇄ kbase_mcp ⇄ HTTP ⇄ KBase API (8100)
```

## 3. 工具契约（三个，粒度从粗到细）

| 工具 | 参数 | 返回 |
|---|---|---|
| `list_knowledge_bases()` | 无 | [{id, name}] |
| `search_knowledge(kb_id, query, top_k=5)` | 纯检索 | [{doc_name, heading_path, text, score}]（复用 /api/kb/{id}/search） |
| `ask_knowledge_base(kb_id, question, provider=None)` | 完整 RAG 问答 | {answer, citations:[{doc_name, heading_path, snippet}]}（内部 drain /query 的 SSE 组装成完整结果——MCP 工具调用返回完整值，不流式） |

错误面：KBase API 不可达 → 工具返回明确中文错误（"KBase 服务未启动，请先启动 …"）；kb 不存在/密钥缺失等 API 错误原样透传 detail。

## 4. 实现形态

- 新顶层包 `kbase_mcp/`（`__main__.py` 入口：`python -m kbase_mcp [--http] [--port 3001]`），依赖官方 `mcp` SDK（FastMCP），加入 pyproject 可选依赖组 `[mcp]`
- 配置走环境变量：`KBASE_API_URL`（默认 http://localhost:8100）；HTTP 传输可选 `KBASE_MCP_TOKEN`（静态 Bearer，设了才校验；STDIO 本地信任不鉴权——完整 API Key 管理体系仍属 v3 商业化加固）
- 工具函数接受注入的 httpx.AsyncClient——测试用 `httpx.ASGITransport(app=create_app(fakes))` 直连内存应用，不起真服务器

## 5. 测试与验收

- 单元/集成：三工具对 ASGI 内存应用（fake embedder/llm）——列表、检索命中、问答含 citations、API 不可达错误文案、token 鉴权（带/不带/错 token）
- 真实验收：起真实 KBase（彩排库），用 mcp SDK 客户端起 STDIO 子进程连接，实调三工具（ask 用真实 provider），断言答案与引用
- README：MCP 章节（Claude Code/Desktop 配置示例 JSON）

## 6. 非目标

流式工具输出（MCP 支持但 v1 客户端体验差异不大，YAGNI）；API Key 完整管理（v3）；MCP resources/prompts 面（工具够用）。

## 7. 搭车项

eval/run_eval.py 的 Generator 用硬编码 min_score=0.3 而非 API 的 rerank 感知取值（M3-1 审查遗留观察）——本周期修正为与 api 相同的取值逻辑。
