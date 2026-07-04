# KBase M2 设计文档 — 设计感前端 + 检索增强

- 日期：2026-07-05
- 状态：已与产品负责人逐节评审通过
- 前置：M1 已合并 main（lite 模式 RAG 骨架，40 测试全绿）

## 1. 目标与范围

M2 是双线版本：

- **工作流一（后端先行）**：检索与摄取增强——混合检索、重排、上下文增强索引、OCR 路由，及前端配套接口（会话存储、文档全文、检索调试、Provider 管理）
- **工作流二（前端收口）**：设计感前端，Vue3 + shadcn-vue 重构，替换 M1 零构建页面

**产品定位约束**：面向大众审美的通用商业产品；政务等行业客制化通过主题令牌实现，不为特定行业预设视觉。

**非目标（明确排除）**：查询改写 QueryRewrite（缓 M3，配置命名空间 `retrieval.rewrite` 预留）、语义缓存/QA对检索器（M3）、GPU OCR 服务化（等 GCP 服务器，接口本次定死）、standard profile 容器编排（M3）、Agent 编排（M4）。

**竞品考察结论（DB-GPT，MIT）**：检索策略分档、Ranker 抽象、标题树检索与本设计方向一致；其 BM25 依赖 Elasticsearch，印证 SQLite FTS5 轻量关键词路的差异化价值；AWEL/SystemApp 重框架机械明确不采纳。

## 2. 检索增强管道（工作流一核心）

### 2.1 数据流

```
查询 ──┬─ 稠密路：bge 向量 → Chroma top-20
       └─ 关键词路：jieba 分词 → SQLite FTS5 (BM25) top-20
              ↓
         RRF 融合（k=60）→ 候选叶子块 top-20
              ↓
         重排（可选）：bge-reranker-v2-m3 交叉编码 → top-5
              ↓
         父块组装 + 去重（M1 逻辑不变）
```

每步可开关，形成四档：纯向量 → 混合 → 混合+重排 → 混合+重排+增强索引。

### 2.2 组件设计

1. **`KeywordIndex` 新组件**（非插槽，内核组件）：SQLite FTS5 虚拟表；中文用 jieba 分词（摄取时叶子块分词入表，查询同分词器）。PG 全文检索留 standard 适配位。**契约清理**：从 `VectorStore` Protocol 移除无人实现的 `keyword_search`
2. **`Reranker` 新插槽**：`rerank(query: str, texts: list[str]) -> list[float]`；默认实现 bge-reranker-v2-m3（sentence-transformers CrossEncoder，本机 CPU 百毫秒级）；`retrieval.rerank.enabled` 全局开关；模型加载失败自动降级为不重排 + healthz 标黄
3. **上下文增强 = 首个 `Enricher` 实现**：摄取时廉价模型（默认 qwen-turbo，`enrich.provider` 可配）为每叶子块生成一句全文定位说明 → `chunks.enrich_context` 新列；向量化文本 = 定位说明 + 标题路径 + 原文；知识库级开关（`knowledge_bases.config` JSON 新列，分块参数一并迁入）；单块增强失败回退为无增强向量化，不阻塞摄取
4. **拒答阈值分模式**：重排开启作用于 rerank 分数（默认 0.35），关闭沿用余弦 0.3；均入配置，评测集校准
5. **重建索引命令**：`python -m kbase.reindex --kb <id>`——补 FTS/增强上下文，基于 Markdown 双存不重新解析原始文件
6. **检索调试**：`POST /api/kb/{id}/search?debug=true` 返回各阶段中间结果（双路排名、融合分、重排前后对比）

### 2.3 数据库变更（启动时轻量迁移，不引入 Alembic）

`chunks.enrich_context TEXT`、`knowledge_bases.config TEXT(JSON)`、FTS5 虚拟表 `chunks_fts`、会话两表（§4.2）、providers 表（§4.2）。迁移函数幂等，缺列检测 ALTER。

## 3. 设计感前端（工作流二）

### 3.1 技术栈与工程结构

Vue3 + Vite + Tailwind CSS + **shadcn-vue**（组件代码复制自持有，Reka UI 无头基座）。独立目录 `web-app/`，构建产物输出 `web/`，FastAPI 静态托管方式不变；M1 零构建页面在 M2 验收后删除。

### 3.2 信息架构（4 页）

1. **问答页**（三栏）：左栏会话历史（时间分组）+ 底部功能导航；中栏对话流——回答直接排版（非气泡），引用为行内角标 `[n]`，答案操作条（引用汇总/复制/检索过程）；右栏引用抽屉——出处路径、命中片段高亮、"查看文档全文"跳转
2. **知识库管理页**：知识库卡片网格 → 文档表格（状态/失败原因/重试/删除）+ 拖拽上传 + 知识库级配置（分块参数、上下文增强开关）
3. **检索分析页**：任意问题跑完整管道，展示双路 top-N、RRF 融合排序、重排前后对比及分数——调优工具兼客户演示素材
4. **设置页**：Provider 卡片管理（增删改 + 连通性测试）、插件健康状态、主题切换

### 3.3 设计系统（`tokens.css`）

- 色彩：暖中性灰阶骨架 + 单强调色（默认靛紫 #534AB7）+ 语义色三组；全部令牌定义亮/暗两套值，暗色主题 = 令牌切换
- 字体：中文栈 system-ui 优先，正文 15px/1.7，字重仅 400/500
- 形状：圆角 8px 控件 / 12px 卡片，4px 间距网格，0.5px 发丝边框
- 客制化出口：行业主题 = 覆盖令牌值的独立 CSS 文件（如政务深蓝主题 ≈ 20 行变量）

### 3.4 组件清单

shadcn-vue 复制引入 ~12 基础件（Button/Input/Select/Dialog/Drawer/Dropdown/Tabs/Table/Toast/Tooltip/Switch/Badge）；自研 5 业务件：MessageStream（流式渲染 + SSE 解析，沿用 M1 已验证的 accumulate-flush 解析逻辑）、CitationChip、CitationDrawer、UploadZone、RetrievalTrace。

## 4. OCR 路由与后端新接口

### 4.1 OCR 路由

- 探测：PDF 在 markitdown 前抽样前 3 页文本密度（默认阈值 50 字符/页，可配）；png/jpg 直接走 OCR 路
- 后端：MonkeyOCR 独立进程 HTTP 服务（本机 CPU 起步，GPU 上线只改 endpoint 配置）；`OCRBackend` 适配器 HTTP 调用
- 状态机扩展：新增 `pending_ocr`（待OCR，非失败）；单个/批量重试接口，前端有重试按钮
- 置信度：低于阈值的文档前端黄色警示，提示人工核对

### 4.2 后端新接口

- **多轮会话**：`conversations`（id/kb_id/title/created_at/updated_at）+ `messages`（id/conv_id/role/content/citations JSON/provider/created_at）；`POST /api/conversations/{id}/query`（SSE）——检索只用当前问题，prompt 附最近 3 轮历史；标题取首问前 20 字
- **文档全文**：`GET /api/documents/{id}/content` 返回 Markdown 全文，支持 heading_path 定位高亮
- **Provider 管理**：providers 入 DB 表（启动时 YAML 种子导入，DB 优先）；API Key 只存环境变量名，永不落库明文；`POST /api/settings/providers/{name}/test` 连通性测试（1 token 请求，返回延迟/错误）
- **检索调试**：见 §2.2-6

## 5. 错误处理

重排模型失败降级不重排 + healthz 标黄；FTS 表缺失启动自动重建；增强调用失败单块回退；OCR 服务不可用转 pending_ocr；会话消息写入与 SSE 流解耦（流中断不丢已生成内容——流结束时落库一次）。

## 6. 测试与验收

- 单元：KeywordIndex 中文精确命中（文件号/专有名词）、RRF 排序正确性、Reranker/Enricher 契约、迁移幂等
- 集成：固定测试集断言 混合 ≥ 纯向量；OCR 路由用程序生成扫描样例；多轮上下文组装
- 评测升级：`run_eval.py` 档位对比模式，一次输出 纯向量/混合/混合+重排 三档命中率表
- 前端：业务件逻辑测试（MessageStream/CitationDrawer）+ 浏览器预览端到端验证（复用 M1 彩排数据）

**M2 完成定义**：评测表 混合+重排 ≥ 混合 ≥ 纯向量；扫描件出结果（CPU 慢速可接受）；四页可用且亮/暗主题可切；旧 `web/` 零构建页移除；全量测试绿。

## 7. 实施顺序

工作流一（后端）→ 工作流二（前端）。前端依赖后端新接口，最后集成。检索增强完成即先跑一轮评测出档位对比表，不等前端。
