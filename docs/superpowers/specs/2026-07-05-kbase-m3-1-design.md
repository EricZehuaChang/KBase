# KBase M3-1 设计文档 — QueryRewrite 与债务批次

- 日期：2026-07-05
- 状态：方向经产品负责人确认（方案3为默认+方案1为可选模式），授权自主执行至上线
- M3 总体拆分：M3-1（本篇：检索收尾+债务）→ M3-2（MCP Server）→ M3-3（内置 Agent 编排）

## 1. QueryRewrite（多轮查询改写）

**目标**：解决多轮指代性追问（"那司局级呢？"）因检索只用当前问题而拒答的问题。

**位置**：仅作用于多轮会话查询 `/api/conversations/{id}/query`；单轮 query 与 search 端点不变。

**数据流**：

```
当前问题 + 最近3轮历史
  → 触发判定（mode 三态）:
      off         恒不触发
      conditional 有历史 且（问题<20字 或 含指代词 或 与历史无关键词重叠）→ 触发（默认）
      always      有历史即触发
  → LLM 改写（廉价模型，复用 enrich.provider 解析机制；asyncio.wait_for 5s）
      prompt：基于对话历史把当前问题改写为自包含的完整检索问题，只输出改写结果
  → 失败/超时/空结果 → 回退原问题（不阻塞不报错，日志 warning）
  → 改写后问题进入既有检索管道（混合/RRF/重排全部不变）
```

**决策**：
1. 改写只影响检索：用户消息原文存库、界面显示原问题；trace 增加 `rewrite: {triggered, original, rewritten}`，检索分析页展示（会话查询无 debug 界面，写日志 + citations 已可佐证；trace 主要供 search?debug 调试路径复现改写时使用——见 §1 实现注记）
2. 新组件 `QueryRewriter`（kbase/rag/rewriter.py），非插槽（仅一种实现，YAGNI）；纯触发判定函数独立可测
3. 配置 `retrieval.rewrite: {mode: "conditional", provider: null, max_wait_s: 5.0}`；指代词表可配置，默认：那/这/它/他/她/其/该/上述/前述/前面/刚才/呢/些/者
4. 生成阶段的历史注入（M2 已有）保持不变

**实现注记**：会话查询链路中改写发生在 `_run_query` 检索之前；`QueryRewriter.rewrite(question, history) -> RewriteResult(query, triggered, rewritten)`。conversations 端点把 RewriteResult.query 传给检索，原问题传给生成与落库。

## 2. 债务批次（六项）

| # | 项 | 设计 |
|---|---|---|
| D1 | DELETE /api/kb/{kb_id} | 级联：Chroma 整集合 delete_collection + keyword_index.delete_kb + chunks/documents 行 + 该库 conversations/messages + files 目录 + kb 行；404 未知库。前端 KB 卡片删除按钮 + 确认 Dialog（显示文档数警示） |
| D2 | conversations 分页 | GET /api/conversations?kb_id=&limit=30&offset=0（updated_at desc）；前端侧栏"加载更多"按钮（剩余条数>0 时显示） |
| D3 | 批量 OCR 重试并发上限 | retry-ocr 改为单个后台任务顺序处理全部 pending_ocr 文档（一个 worker 槽位，不再每文档一个 task），响应返回 {queued: n} |
| D4 | 去重唯一约束 | 迁移加 UNIQUE INDEX (kb_id, content_hash)；ingest 捕获 IntegrityError → 重查返回既有 doc id（消除并发重复摄取竞态） |
| D5 | 摄取并行化 | 批量上传由"每文件一个 bg task（串行）"改为单 bg task 内 ThreadPoolExecutor 并行摄取；`ingest.workers` 配置默认 2（CPU embedding 受益，失败隔离语义不变） |
| D6 | 巨型父块尺寸上限 | retriever._assemble 中父块文本超 `retrieval.max_parent_chars`（默认 4000）时，以命中叶子在父块中的位置为中心截窗（前后补省略号标记），保 LLM 上下文不被单块撑爆 |

## 3. 验收标准

- 真实端到端：彩排库多轮会话先问"出差北京住宿费标准是多少？"再追问"那司局级呢？"——第二问不再拒答且答案正确（650 元），citations 命中住宿标准文档
- rewrite 三态模式均有测试（off 不触发/conditional 按条件/always 恒触发）；失败回退有测试
- D1-D6 各有测试；D1 删除后 Chroma 集合与 FTS 行确实清空；D4 并发同文件摄取只产生一行
- 后端/前端全量测试绿；前端新增 UI（KB 删除、会话加载更多）过既有设计纪律检查

## 4. 非目标

语义缓存（缓后，失效机制另行设计）；rewrite 的设置页 UI 开关（配置文件足够，M3-3 后视需要）；C 方向生产化部署项。
