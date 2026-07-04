# KBase 商业化知识库系统 · 设计文档

- 日期：2026-07-04
- 状态：已与产品负责人逐节评审通过
- 首个交付节点：2026-07-08（下周三）技术测试演示

## 1. 产品定位

私有化交付的 B 端知识库系统（代号 KBase），单租户，Docker Compose 一键部署。

核心卖点：

1. 标准 RAG 能力开箱即用（摄取 → 分块 → 索引 → 检索 → 生成，全链路带引用溯源）
2. 全链路组件可配置：Embedding / 向量库 / LLM / OCR / 分块器 / 增强器均为插槽
3. 原生 Agent 协作：对外可被 Agent 当工具用（MCP Server），对内可用 Agent 编排复杂场景（v2）

首个客户场景（兵团政策 AI 助手）验证三类应用：政策方案生成（v2）、政策定期汇编（v2）、财务报销 Q&A（v1 RAG 直接覆盖）。产品定位为通用知识库系统，不与单一客户场景耦合。

## 2. 版本分期

| 版本 | 范围 |
|---|---|
| v1 | RAG 骨架（含混合检索、父子分块、引用溯源）+ MCP Server + Web UI（问答/知识库管理/设置）|
| v2 | 内置 Agent 编排：政策方案生成（意图理解→多轮检索→分节生成→自检）、定期汇编流水线 |
| v3 | 商业化加固：用户体系与细粒度权限、审计日志、许可证管理、运营报表 |

v1 的 API 与数据模型为 v2/v3 预留：prompt 模板表、任务表、API Key 表。

## 3. 总体架构

```
┌──────────────────────────────────────────────────┐
│ 接入层   Web UI（管理+问答）/ REST API / MCP Server │
├──────────────────────────────────────────────────┤
│ 应用层   问答(RAG) / 汇编(v2) / 方案生成(v2 Agent)   │
├──────────────────────────────────────────────────┤
│ 内核层   摄取管道 → 分块 → 索引 → 检索(+重排) → 生成  │
├──────────────────────────────────────────────────┤
│ 插件层   Embedder / VectorStore / LLMProvider /    │
│          OCRBackend / Chunker / Enricher           │
├──────────────────────────────────────────────────┤
│ 存储层   向量库 / 元数据库 / 原始文件+Markdown 仓     │
└──────────────────────────────────────────────────┘
```

关键决策：

- **技术路线**：自研接口层 + 精选原子库。不引入 LlamaIndex/LangChain 框架本体；成熟原子能力用单包（如 `langchain-text-splitters`）。理由：私有化涉密交付要求依赖可审计；v2 Agent 编排长在自有内核上；商业授权干净。若后续需要框架内某实现，通过插槽适配器包装引入。
- **内核层只依赖抽象接口**，具体实现在插件层注册，YAML 配置选择实现。
- **文档双存**：原始文件 + 转换后 Markdown 均保留，Markdown 为系统标准中间格式，重建索引/换分块策略不重新解析原始文件。
- **摄取复用 markitdown**（pip 依赖，非 fork），扫描件走 OCR 插槽。

## 4. 性能设计（目标：≥100 并发问答用户）

量级换算：100 并发在线 ≈ 10~20 QPS 检索 + 20~50 路并发 LLM 流式生成。

| 环节 | 对策 |
|---|---|
| LLM 生成（主瓶颈）| 全链路异步 + SSE 流式；LLM 网关按 Provider 配置最大并发、超额排队（不打爆下游）；在线 API 阶段多 Key 轮转，私有化阶段 vLLM continuous batching（72B 在 2×A100/H20 上 30~50 路并发为成熟实践，可作为客户 GPU 预算依据）|
| Embedding | 独立容器服务（HuggingFace TEI，自带动态 batching，原生支持 bge 系列），API 层 HTTP 调用 |
| 向量检索 | 生产默认 Qdrant 独立容器；该量级下检索不构成瓶颈 |
| 元数据 | 生产默认 PostgreSQL（会话历史高频写入，SQLite 单写者模型不适用）|
| API 层 | 无状态（会话状态全部落库），`docker compose scale api=N` + nginx 横向扩展；查询 Embedding 缓存；可选语义缓存（政策问答重复率高）|

### 部署 Profile（同一套代码，compose profile 切换）

| | lite（演示/POC）| standard（生产，100+ 并发）|
|---|---|---|
| 元数据 | SQLite | PostgreSQL |
| 向量库 | Chroma 嵌入式 | Qdrant（或 pgvector/Zvec 适配器）|
| Embedding | 进程内加载 | TEI 独立服务 |
| LLM | 在线 API | vLLM 集群 / 在线 API |
| 容器数 | 1~2 | 5~6，API 可 scale |

## 5. 内核层设计

### 5.1 摄取管道

```
上传/批量导入 → 格式检测 → 解析路由 → 标准 Markdown → 元数据抽取 → 入库
```

- 解析路由：有文本层格式走 markitdown；PDF 先做文本层探测（抽样页字符密度），扫描件走 OCR 插槽（默认 MonkeyOCR HTTP 服务，可配 LLM Vision）
- 异步任务队列：摄取为后台任务（起步 FastAPI BackgroundTasks + 任务状态表，量大切 arq/Celery，接口不变），UI 展示每篇文档解析状态
- 失败隔离：单篇失败只标记该篇（记录原因、可重试），不影响批次
- OCR 后端不可用时文档标记"待 OCR"（非失败），GPU 服务器启动后批量重试——适配按需启停的 GPU 服务器
- 去重：文件内容 hash，重复上传秒完成并提示

### 5.2 分块（含语义歧义对策）

问题本质：检索需要小块（命中准），生成需要大块（上下文完整）。解法是两个粒度解耦。

**v1 默认启用（零额外成本）：**

1. **父子分块（small-to-big）**：小粒度（默认 512 token，可配）切块做向量化；每块记录 `parent_id / prev_id / next_id`；检索命中后向上取父章节或向两侧扩展邻近窗口，再送生成。向量检索精准命中小块，LLM 看到完整上下文，指代不悬空。
2. **标题结构分块**：沿 Markdown 标题层级切分，chunk 携带标题路径（如 `文件名 > 第三章 > 第十二条`），路径拼入 chunk 文本参与向量化；超长节按段落递归切分带重叠窗口。实现：`langchain-text-splitters` 的 MarkdownHeaderTextSplitter + RecursiveCharacterTextSplitter。

**可选增强（知识库级配置开关）：**

3. **上下文增强索引（Contextual Retrieval）**：摄取时 LLM 为每块生成一句全文定位说明，拼在块前再向量化。效果最好但摄取有 LLM 调用成本（用小模型），适合高价值低更新库。实现为摄取管道的 Enricher 环节。
4. **语义分块**：embedding 相似度找断点，适合非结构化文档（调研报告、会议记录），作为 Chunker 插槽另一实现，默认关。

### 5.3 索引与检索

- **混合检索**（政策场景关键：文件号/政策名/专有名词是精确词命中）：稠密向量（bge-m3）+ 关键词索引（PG 全文检索 / Qdrant sparse vector）双路，RRF 融合
- **重排插槽**（可选）：bge-reranker-v2-m3 跑在 TEI，top-20 精排 top-5；lite 默认关，standard 默认开
- **元数据过滤**：知识库 / 文档集 / 标签 / 时间范围

### 5.4 生成

- 上下文组装：命中块按来源分组编号注入 prompt，要求 LLM 标注引用 `[1][2]`
- 答案带引用：返回 citation 列表（文档名、标题路径、原文片段），UI 可展开看原文——涉密场景信任基础，应对"10% 错误率"交付风险的核心手段
- 拒答机制：检索相关度低于阈值时明确回答"知识库中未找到依据"，不允许模型编造；拒答走正常响应路径并在日志单独归类（知识库覆盖度运营指标）
- Prompt 模板配置化存库，问答/汇编/方案生成各自独立模板（v2 挂点）

## 6. 插件层接口契约

模式：抽象基类定义契约 → 实现注册到注册表 → YAML 配置选择。内核只 import 抽象类。

```python
class Embedder(Protocol):
    def embed(texts: list[str]) -> list[Vector]   # 批量接口
    dimension: int                                 # 建库时校验

class VectorStore(Protocol):
    def upsert(collection, chunks_with_vectors)
    def search(collection, vector, top_k, filters) -> list[Hit]
    def keyword_search(collection, query, top_k, filters) -> list[Hit]
    def delete(collection, doc_id)

class LLMProvider(Protocol):
    def stream(messages, model, **params) -> AsyncIterator[str]
    def complete(messages, model, **params) -> str

class OCRBackend(Protocol):
    def to_markdown(file_path) -> OCRResult        # Markdown + 置信度

# Chunker / Enricher 见 5.2
```

v1 自带实现与扩展位：

| 插槽 | v1 实现 | 扩展位 |
|---|---|---|
| Embedder | TEI(bge-m3)、OpenAI 兼容 API | 任意 |
| VectorStore | Qdrant、Chroma(lite) | Zvec、pgvector |
| LLMProvider | OpenAI 兼容（通吃 DeepSeek/Qwen/硅基流动/vLLM，base_url+model 区分）| Anthropic 原生等 |
| OCRBackend | MonkeyOCR(HTTP)、LLM Vision | PaddleOCR |
| Chunker | 结构分块 | 语义分块 |
| Enricher | 上下文增强 | 摘要、关键词抽取 |

LLM 配置示例（运行时可切，UI 按会话切换，支撑不同规模模型对比测试）：

```yaml
llm:
  providers:
    - name: qwen-72b
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      model: qwen2.5-72b-instruct
      max_concurrency: 20
    - name: qwen-32b
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      model: qwen-plus   # 或任意 OpenAI 兼容端点的中档模型，对比测试用
  active: qwen-72b
```

## 7. 接入层

### 7.1 REST API（OpenAPI 自动文档）

```
POST /api/kb                        建知识库（含分块/检索策略配置）
POST /api/kb/{id}/documents         批量上传（返回任务 id）
GET  /api/kb/{id}/documents         文档列表与解析状态
POST /api/kb/{id}/query             检索问答（SSE 流式，答案+引用）
POST /api/kb/{id}/search            纯检索（命中块，供外部二次加工）
POST /api/chat                      多轮会话（挂载 1~N 知识库）
GET/PUT /api/settings/*             模型/插件配置管理
GET  /healthz                       各插件健康状态
```

### 7.2 MCP Server（独立进程，STDIO + Streamable HTTP）

- `search_knowledge(kb, query, filters)`：纯检索，返回带出处原文块
- `ask_knowledge_base(kb, question)`：完整 RAG 问答，答案+引用
- `list_knowledge_bases()`：能力发现

认证：API Key（库内可管理、可吊销），每个对接方一把，v3 审计挂点。

### 7.3 Web UI（v1 范围）

- 问答页：会话式问答、引用展开看原文、模型切换下拉、多轮历史
- 知识库管理页：建库、拖拽批量上传、文档状态、失败重试
- 设置页：LLM Provider 管理、插件配置、API Key 管理
- 选型：Vue3 + 组件库，前后端分离、同容器交付（nginx 静态托管）

**v1 明确不含**：用户体系与细粒度权限（v3）、Agent 编排画布（v2）、汇编/方案生成场景页（v2）。

## 8. 错误处理

原则：故障隔离在最小单元；异步过程有状态可查；用户永远知道当前状态。

- 摄取：单篇失败不传染批次；OCR 不可用转"待 OCR"可批量重试
- LLM：超时/限流自动重试一次，仍失败明确报错并提示切换模型；**不做静默降级**（用户必须知道答案出自哪个模型）
- 检索：向量库不可达为系统级故障，`/healthz` 暴露插件状态，compose 配置依赖与重启策略
- 拒答不是错误：正常响应路径 + 日志单独归类

## 9. 测试策略

1. **单元测试**：六插槽契约测试——同一套用例跑所有适配器，新适配器过契约测试即可上线
2. **管道集成测试**：用 `RAG/材料` 脱敏文档做固定测试集，摄取→检索全链路断言关键问题命中正确块；分块策略改动的回归保护
3. **检索质量评测集**：20~30 问答对，不进 CI，单命令输出命中率/引用准确率报告——同时是客户演示"模型规模对比"的产出物

## 10. 下周三 Demo 裁剪清单

交付窗口：周末 + 周一 + 周二（约 4 个工作日）。

**必须有：**

1. lite 模式一键起（SQLite + Chroma + 进程内 bge）
2. 批量导入脱敏文档（markitdown + 结构分块 + 父子块）
3. 问答页：流式回答 + 可展开引用
4. 模型切换下拉 + 评测集对比报告（回答"36B vs 72B"问题）

**明确不做（周三后按 v1 计划补齐）：**

- OCR 路由（演示文档选有文本层的）
- MCP Server、混合检索、重排、上下文增强
- standard 模式容器编排（PG/Qdrant/TEI）
- 设置页 UI（模型配置先改 YAML）

Demo 代码全部生长在 v1 正式架构骨架上，无丢弃式代码。

## 11. 成功标准

- Demo（7/8）：现场导入脱敏文档后问答可用、引用可溯源、两个规模模型可切换对比且有量化报告
- v1：三类插槽各 ≥2 个实现通过同一契约测试；lite/standard 两 profile 均一键起；MCP 工具可被 Claude Code 实际调用完成检索
- 性能：standard profile 下 100 并发用户问答，检索 P95 < 500ms（不含 LLM 生成），系统无错误
