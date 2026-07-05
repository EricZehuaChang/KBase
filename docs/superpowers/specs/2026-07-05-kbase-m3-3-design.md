# KBase M3-3 设计文档 — 内置 Agent 编排（方案生成与定期汇编）

- 日期：2026-07-05
- 状态：授权自主执行（M3 总授权内）
- 前置：M3-2 已合并（119 backend / 34 frontend）

## 1. 目标

落地客户场景 1、2（原始 spec v2 承诺）：
- **方案生成**：输入主题与要求 → 生成大纲（可编辑）→ 逐节检索+生成带引用 → 汇整成文档（Markdown + Word 导出）
- **定期汇编**：选定知识库文档集 → 逐文档摘要 + 总览 → 汇编文档（同导出）

定位为**内置轻量编排**：自研顺序步骤执行器，不引入 Agent 框架（与"轻内核"哲学一致；DB-GPT 考察时已明确不采纳 AWEL 类重机械）。

## 2. 架构

```
POST /api/proposals/outline（同步，~15s 一次 LLM 调用）→ 大纲 JSON（前端可编辑）
POST /api/jobs（type=proposal|digest, params 含大纲/文档集）→ job id（后台执行）
GET  /api/jobs/{id}（轮询：status/progress/逐节完成态）
GET  /api/jobs/{id}/artifact?format=md|docx（产物下载）
```

- **jobs 表**：id/kb_id/type/status(pending→running→done|failed)/params JSON/progress JSON/artifact_path/error/created_at/updated_at。迁移沿轻量机制。
- **JobRunner**（kbase/jobs/runner.py）：顺序执行步骤列表，每步完成即更新 progress（фsection 粒度：`{steps:[{name,status,detail}]}`），异常捕获→status=failed+error；BackgroundTasks 承载（与摄取同模式）。
- **ProposalFlow**（kbase/jobs/proposal.py）：`outline(topic, requirements, kb)` 一次 LLM 调用（附检索到的 top-5 相关块作背景）产出 JSON 大纲 `[{title, brief}]`；`generate_section(section, kb)` 每节：以"主题+节标题+brief"检索 top-5 → 复用 Generator 的 prompt 风格生成节文（非流式 complete，带 [n] 引用）→ 收集 citations；`assemble()` 合并为 Markdown：标题/各节/末尾"引用文献"附录（全局重编号，节内 [n] 映射到全局编号）。
- **DigestFlow**（kbase/jobs/digest.py）：对选定 doc_ids（缺省=全库 ready 文档）逐文档取 content.md 头部+若干块 → 每文档一段摘要（LLM complete）→ 总览段 → 汇编 Markdown。
- **导出**（kbase/jobs/export_docx.py）：python-docx（新依赖），支持 H1-H3/段落/无序列表/粗体的最小 Markdown→docx 转换；产物落 `data/jobs/{id}/artifact.{md,docx}`（md 即真源，docx 按需转出）。
- **前端第 5 页「生成」**：Tab 切换 方案生成/定期汇编。方案向导：表单（KB/主题/要求）→ 大纲编辑器（节列表增删改序）→ 生成（进度=节清单逐项打勾，轮询 3s）→ 产物 Markdown 预览 + 下载 md/docx。汇编：文档多选 → 生成 → 同进度/产物 UX。任务历史列表（该 KB 的 jobs）。

## 3. 关键决策

1. **大纲同步、正文异步**：大纲是单次调用且用户要立即编辑，同步返回（前端 60s 超时）；逐节生成分钟级，走 jobs 后台+轮询（不做 SSE token 流——产物是文档不是对话，节粒度进度足够，YAGNI）
2. **引用全局重编号**：各节独立检索得到的 [n] 在汇整时映射到全局引用表（按 doc_name+heading_path 去重），保证成品文档引用编号唯一一致——这是"可溯源"卖点在长文档形态的延伸
3. **失败恢复**：单节生成失败标记该节 failed 并继续后续节（同摄取的失败隔离哲学）；产物中失败节留占位说明；job 整体 done_with_errors 状态（status 枚举 +1）
4. **provider**：jobs 用请求指定 provider（缺省 active）；大纲与正文同 provider

## 4. 测试与验收

- 单元：JobRunner 步骤推进/失败捕获/进度写；outline JSON 解析（LLM 输出鲁棒解析：容忍 markdown 代码块包裹）；引用全局重编号映射；docx 转换（标题/段落/列表落到 docx 结构）；digest 逐文档
- 集成：FakeLLM 全流程 proposal job（TestClient，bg 同步语义）→ done、artifact md 存在、引用附录存在；digest 同
- 真实验收：彩排库生成一份"师市人才引进住房保障实施方案"（3-5 节），检查产物结构/引用/docx 可开；汇编对 5 份 ready 文档跑通
- 前端：向导流转 vitest（大纲编辑纯函数）+ 浏览器终验（B8 模式）

## 5. 非目标

内置定时调度（部署层 cron 调 API 即可）；多 job 并发执行器（BackgroundTasks 天然并发，量级足够）；产物版本管理；模板系统（M4 若客户要）。
