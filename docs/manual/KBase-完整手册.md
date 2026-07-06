# KBase 私有化知识库系统 —— 完整使用手册

> 本手册面向 KBase 的客户管理员与最终用户，介绍产品能力、部署方式、日常使用与运维方法。如需接入自有系统或 Agent，请参阅第 7 章「集成手册」。

---

## 目录

1. [产品概述](#1-产品概述)
2. [快速开始](#2-快速开始)
3. [用户手册](#3-用户手册)
4. [管理员手册](#4-管理员手册)
5. [部署手册](#5-部署手册)
6. [运维手册](#6-运维手册)
7. [集成手册](#7-集成手册)
8. [附录](#8-附录)

---

## 1. 产品概述

### 1.1 定位

KBase 是一套**私有化部署**的智能知识库系统（RAG，Retrieval-Augmented Generation，检索增强生成）。系统部署在客户自有环境中，所有文档、向量索引、对话记录均保存在客户基础设施内，**数据不出域**。

KBase 是通用知识库产品，不限定于某个行业或场景。典型应用场景包括：

- **政策方案生成**：输入主题与要求，系统检索知识库相关依据，自动生成结构化方案文档（大纲 → 分节撰写 → 引用整理），支持导出 Word。
- **政策摘编汇编**：对一批文档批量生成摘要与总览，产出汇编文档。
- **财务报销问答**：员工就报销标准、审批流程等提出自然语言问题，系统检索内部规章制度并给出带引用的回答。

除以上场景外，任何需要"上传文档 → 检索 → 问答/生成"的知识管理场景都可以直接使用 KBase。

### 1.2 能力总览

| 能力域 | 说明 |
|---|---|
| 多格式文档摄取 | 基于 `markitdown` 解析 .docx / .pdf / .md / .xlsx / .pptx 等常见格式；扫描件/图片通过 OCR（Optical Character Recognition，光学字符识别）服务转换为可检索文本 |
| 父子分块检索 | 小粒度分块保证检索精准命中，命中后自动展开父级章节，保证生成时上下文完整 |
| 混合检索 | 向量语义检索（稠密路）+ 关键词全文检索（BM25 / PostgreSQL tsquery）双路并行，RRF（Reciprocal Rank Fusion，倒数排名融合）算法融合排序 |
| 重排 | 交叉编码器（Cross-Encoder）二次精排，检索质量更高；支持过载自适应降级并可通过 `/healthz` 观测 |
| 防幻觉双门控 | 拒答门（相关度不足时明确告知未找到依据，不编造）+ 收录门（保留同一问题的多份证据，包括互相冲突的版本，交给模型辨析） |
| 多轮对话 | 会话历史管理 + 查询改写（QueryRewrite，让"那司局级呢？"这类指代性追问也能正确检索） |
| 流式问答与溯源 | SSE（Server-Sent Events）流式输出 + 引用角标可点击查看原文片段、相关度分数 |
| 检索分析 | 稠密路 / 关键词路 / 融合 / 重排四路结果并列对比，供调优与演示使用 |
| 生成编排 | 方案生成（大纲 → 逐节生成 → 引用全局重编号 → md/docx 导出）、定期汇编（多文档摘要） |
| 模型热切换 | 支持通义千问（qwen-plus/qwen-max）、DeepSeek-V3、qwen3-32b 等模型运行时切换对比 |
| 安全与权限 | JWT Cookie + API Key 双通道鉴权、三级角色、审计日志、Ed25519 签名许可证 |
| Agent 协作 | 内置 MCP（Model Context Protocol）Server，外部 Agent（如 Claude Code / Claude Desktop）可直接调用知识库能力 |

### 1.3 双部署形态

KBase 采用同一套内核代码，通过配置切换两种部署形态：

| | lite（轻量） | standard（标准） |
|---|---|---|
| 适用场景 | 演示、小规模验证、单机环境 | 生产环境、较高并发 |
| 元数据库 | SQLite（单文件） | PostgreSQL |
| 向量库 | Chroma（嵌入式，进程内） | Qdrant（独立容器） |
| 向量化/重排 | 进程内加载 bge-m3 / bge-reranker | 独立 TEI（Text Embeddings Inference）服务 ×2 |
| 容器数 | 1 个 | 5 个（app + postgres + qdrant + tei-embed + tei-rerank） |
| 部署方式 | `docker compose -f docker-compose.lite.yml up -d --build` | `docker compose -f docker-compose.standard.yml up -d --build` |

两种形态用 Docker Compose 一键启动，无需额外安装步骤。详见第 5 章。

### 1.4 架构简介

KBase 内核采用分层 + 插件化设计，各层职责如下：

```
接入层   Web UI（问答/知识库/检索分析/生成/设置）/ REST API / MCP Server
应用层   问答(RAG) / 定期汇编 / 方案生成（内置编排）
内核层   摄取管道 → 分块 → 索引 → 检索(+重排) → 生成
插件层   Embedder / VectorStore / LLMProvider / OCRBackend / Chunker / Enricher
存储层   向量库 / 元数据库 / 原始文件与 Markdown 仓
```

内核层只依赖抽象接口，具体实现（向量化模型、向量库、大模型、OCR 后端等）在插件层注册，通过 YAML 配置选择实现。这意味着：

- 更换向量库（如从 Chroma 换成 Qdrant）、更换大模型服务商，都只需改配置，不需要改动业务代码——lite 与 standard 两种部署形态正是这一设计的直接体现。
- 客户如有自建的向量库、私有化大模型网关等基础设施，理论上可以通过实现对应插件接口接入，无需等待产品原生支持（具体接入需求请联系交付团队评估）。

---

## 2. 快速开始

本章给出 lite 模式下最短的开箱路径：环境准备 → 启动 → 首次登录 → 建库 → 上传文档 → 提问。

### 2.1 环境要求

- 已安装 Docker 与 Docker Compose（Docker Desktop 或独立安装均可）。
- 一台可访问互联网的机器（用于首次拉取镜像、下载模型权重、调用大模型 API）；如为纯内网环境，需提前准备好镜像与模型缓存。
- 一个可用的 DashScope（阿里云百炼）API Key，或其他 OpenAI 兼容的大模型网关密钥。

### 2.2 准备 `.env` 文件

在项目根目录创建 `.env` 文件（不会被提交到版本库）：

```bash
KBASE_SECRET_KEY=请填一个足够随机的长字符串    # 会话签名密钥，必填
KBASE_ADMIN_PASSWORD=请设置首启管理员密码       # 可选，不设则随机生成并打印到日志
DASHSCOPE_API_KEY=你的百炼平台API密钥           # 必填
```

> **`KBASE_SECRET_KEY` 为什么必填**：这是登录会话 Cookie 的签名密钥。如果不设置，每次重建容器都会生成新密钥，导致所有已登录用户的登录状态失效，被迫重新登录。lite/standard 的 compose 文件都会在未设置该变量时直接拒绝启动，提醒你补上。

### 2.3 一键启动（lite 模式）

```bash
docker compose -f docker-compose.lite.yml up -d --build
```

首次启动会从 HuggingFace 下载 bge-m3（向量化模型）与 bge-reranker-v2-m3（重排模型）的权重文件，体积较大，请耐心等待（已用命名卷持久化，重建容器不会重新下载）。

**国内网络加速**：如果模型下载缓慢，可在 `docker-compose.lite.yml` 的 `app` 服务 `environment` 段加入：

```yaml
HF_ENDPOINT: https://hf-mirror.com
```

镜像拉取缓慢时，也可配置 Docker 的 `registry-mirrors`，或将 compose 中的镜像名换成对应的国内加速仓库地址（两者任选其一，不要同时改）。

启动完成后，浏览器访问 `http://localhost:8100`。

### 2.4 首次登录

首次启动时系统会自动创建一个 `admin` 账号：

- 如果你在 `.env` 中设置了 `KBASE_ADMIN_PASSWORD`，用该密码登录。
- 如果没有设置，系统会随机生成一个 16 位密码并打印到容器日志（仅打印这一次）：

```bash
docker compose -f docker-compose.lite.yml logs app
```

在日志中找到 `kbase.auth.bootstrap` 相关的 WARNING 行，取得初始密码后登录，**建议立即在设置页修改密码或新建其他账号**。

### 2.5 建立知识库并上传文档

1. 登录后进入「知识库」页面，点击「新建知识库」，输入名称。
2. 进入该知识库详情页，将文档拖拽到上传区域（或点击选择文件）。
3. 支持的格式包括 .docx / .pdf / .md / .xlsx / .pptx 等常见办公文档格式；**旧版二进制 .doc 格式不受支持，请先转换为 .docx 再上传**。
4. 文档会先进入"解析中"状态，解析完成后变为"就绪"。扫描件/图片文档会先尝试 OCR 识别，可能出现"待OCR"状态（见 3.3 节说明）。

### 2.6 开始提问

进入「问答」页面，选择刚才建好的知识库，输入问题并发送。系统会流式输出回答，回答中形如 `[1]` 的角标是引用标记，点击可查看引用来源片段与相关度分数。

至此最短路径完成。更完整的功能说明见第 3 章「用户手册」。

---

## 3. 用户手册

本章按页面走查 KBase 的功能，面向日常使用的最终用户（问答、检索、生成场景）与内容维护人员（知识库管理）。

### 3.1 登录

访问系统地址后，未登录用户会被重定向到登录页。登录页居中展示 KBase 标题、"登录以继续"提示，输入**用户名**与**密码**后点击「登录」。

- 用户名或密码为空时，页面会提示"请输入用户名和密码"，不会发起请求。
- 用户名密码错误或账号已被禁用时，会返回明确的错误提示。
- 登录成功后会话通过 HttpOnly Cookie（`kbase_session`）维持，有效期 7 天。

### 3.2 问答页

问答页是最常用的入口，左侧是会话列表，右侧是对话区。

**会话列表**：按更新时间自动分组为"今天"/"7 天内"/"更早"三组（空分组不展示）。列表顶部有「新会话」按钮；列表底部在还有更多历史会话时显示「加载更多」。

**提出问题**：在底部输入框输入问题，回车发送（Shift+Enter 换行）。发送后：

1. 系统先返回一批引用（citations），再流式返回回答文字（token 逐段追加），最后返回结束信号。
2. 回答区域在生成过程中显示"思考中…"，直至第一段文字到达。
3. 如果连接中途异常中断，回答末尾会追加"⚠️ 回答中断，请重试"提示。

**引用抽屉**：回答中形如 `[1]`、`[2]` 的角标是可点击的引用标记，点击后从右侧滑出引用抽屉，展示：

- 来源文档名与标题路径（如"文件名 > 第三章 > 第十二条"）；
- 命中片段原文，问题中的关键词会高亮显示；
- 相关度分数（3 位小数）；
- 「查看文档全文」按钮，点击可在弹窗中查看整篇文档 Markdown 全文，并自动定位高亮到命中的标题位置。

**多轮追问**：同一会话内继续提问，系统会自动携带最近 3 轮历史辅助生成回答。对于"那司局级呢？"这类依赖上一轮语境、单独检索容易失败的追问，系统内置**查询改写**（QueryRewrite）机制：检索前用大模型把追问改写为语义完整、可独立检索的问题（仅影响检索这一步，你在界面上看到的问题文字、会话历史记录都是你输入的原文，不会被改写替换）。

**模型切换**：页面顶部可选择当前会话使用的知识库和大模型 Provider（如 qwen-plus / qwen-max / deepseek-v3 等），切换后立即生效，方便对比不同模型对同一问题的回答质量。

**拒答行为**：当检索到的内容与问题相关度不足（低于系统配置的阈值）时，系统不会编造答案，而是直接回复：

> 知识库中未找到依据，无法回答该问题。请尝试换个问法，或确认相关文档已导入。

这条拒答文本以普通回答的形式流式返回，没有特殊颜色标记——如果你频繁看到这句话，通常说明知识库里确实缺少相关文档，而不是系统故障。

**回答操作条**：回答完成后，下方会出现操作按钮：显示引用数量的小标签（如"3 条引用"）、「复制」按钮（复制回答文字到剪贴板）、「检索过程」按钮（跳转到检索分析页，用同一个问题重新执行一次检索，查看详细过程）。

### 3.3 知识库管理页

#### 建库与文档管理

「知识库」页面默认展示卡片网格，每张卡片显示知识库名称与文档数量。点击「新建知识库」输入名称即可创建；点击已有卡片进入该知识库的文档详情页。

进入详情页后可以：

- **拖拽上传**：将文件拖入上传区域，或点击选择文件，支持批量上传。上传后文档先以"解析中"状态乐观显示，随后被真实状态覆盖。
- **知识库配置**：点击「知识库配置」按钮，可调整分块大小（chunk_size，64-4096，默认 512）、分块重叠（chunk_overlap，0-512，需小于分块大小，默认 64）、以及是否开启上下文增强（enrich，为每个分块生成一句全文定位说明后再向量化，能提升检索精度但会增加摄取时的大模型调用成本）。**配置修改仅影响后续新上传的文档**，不会对已入库的文档重新生效。

#### 文档状态说明

| 状态 | 含义 |
|---|---|
| 等待中（pending） | 已接收，尚未开始解析 |
| 解析中（parsing） | 正在提取内容、分块、向量化 |
| 就绪（ready） | 摄取完成，可被检索 |
| 待OCR（pending_ocr） | 文档是扫描件/图片，OCR 服务当前不可达或未就绪；**不会阻塞同批次其他文档的摄取**，OCR 服务恢复后可重试补跑 |
| 失败（failed） | 解析出错（如文件损坏、格式不受支持），失败原因可在状态旁查看 |

对于"失败"和"待OCR"状态的文档，行内会出现重试按钮，点击可对单个文档重新发起摄取。知识库详情页顶部还有「批量重试OCR」按钮（仅在存在待 OCR 文档时出现），一次性重试该知识库下所有待 OCR 文档。

> **为什么会出现"待OCR"**：OCR 依赖独立部署的 MonkeyOCR 服务，与主系统生命周期分离。当 OCR 服务未启动、地址配置错误或临时不可达时，扫描件类文档会优雅降级为"待OCR"而不是直接判失败，避免因为一个外部服务的问题连累整批文档摄取失败。

#### 删除

文档和知识库都支持删除，删除前会弹出确认对话框：

- 删除文档：确认后级联清理该文档在向量库、关键词索引、数据库中的记录及原始文件。
- 删除知识库：确认对话框会显示将要删除的文档数量提示，级联清理该知识库下全部文档、分块、向量集合、关键词索引、会话记录及原始文件目录，操作不可恢复。

### 3.4 检索分析页

检索分析页用于查看一次检索请求在系统内部各阶段的表现，适合排查召回质量问题，也是向客户演示系统检索能力的素材。

选择知识库、输入查询语句后点击「执行」，页面并列展示三到四栏结果：

1. **稠密路 top-10**：纯向量语义检索的结果与分数。
2. **关键词路 top-10**：BM25/全文检索的结果与分数（若该检索路未启用，显示"未启用"）。
3. **RRF 融合 top-10**：两路结果按 RRF 算法融合后的排序；同时被稠密路和关键词路命中的结果会有特殊标记，表示"双路共识"。
4. **重排 top-10**（若启用了重排）：显示重排后的最终排序，并标注每条结果相对融合排序的名次变化——上升（↑N）、下降（↓N）、新进入榜单（新进，指重排前不在融合 top-10 但重排后进入的结果）、名次不变（—）。

页面底部展示最终会被送入生成阶段的结果块（blocks），可展开查看完整原文。

**用途**：当用户反馈"这个问题查不到"或"答案不对"时，可以用同样的问题在此页面复现检索过程，判断问题出在检索召回不足、关键词路未命中、还是重排把正确结果排到了后面。

### 3.5 生成页

生成页提供两类"批量生成"能力：方案生成与定期汇编，均以后台任务形式运行，可随时关闭页面稍后回来查看进度。

#### 方案生成向导

1. **填写主题与要求**：选择知识库，输入主题（如"某师市人才引进住房保障实施方案"）和补充要求（依据的政策范围、篇幅、侧重点等），可选择指定大模型 Provider，点击「生成大纲」。
2. **编辑大纲**：系统同步返回一份 3~7 节的大纲，每节包含标题与要点简述，均可编辑；支持增删节、调整标题与要点文字、调整顺序。确认无误后点击「开始生成」。
3. **查看生成进度**：进度页展示逐节生成的进度清单，每节状态实时更新（完成/失败/进行中/等待中）。某一节如果检索不到可用依据，系统不会编造内容，而是在该节写入"知识库中无相关依据，本节未生成"的占位说明，不影响其余节继续生成。
4. **下载产物**：全部节生成完成后，可下载 Markdown 或 Word（.docx）格式的产物。正文中的引用编号会在汇整阶段全局重新编号去重，文末附"引用文献"列表。

任务终态包括：**已完成**（全部节成功）、**部分完成（存在失败步骤）**（部分节失败但仍有产出）、**生成失败**（任务级错误）。

#### 定期汇编

选择知识库后，可勾选要纳入汇编的文档（默认为该知识库下全部"就绪"状态的文档），点击「生成汇编」。系统对每篇文档生成一段摘要，全部完成后再生成一段总览，最终产出"文档汇编"格式的 Markdown/Word 文档。

#### 任务历史

生成页下方保留该知识库下的历史任务列表，可随时查看以往任务的产物并重新下载。

---

## 4. 管理员手册

本章内容仅 admin 角色可见/可操作，涉及用户与角色管理、API Key、许可证、审计日志与模型 Provider 管理，均在「设置」页完成。

### 4.1 用户与角色

#### 三级角色权限表

| 能力 | viewer（查看者） | editor（编辑者） | admin（管理员） |
|---|:---:|:---:|:---:|
| 查看知识库/文档列表、检索、问答（含会话） | ✅ | ✅ | ✅ |
| 建库、上传/删除文档、重试解析/OCR、发起生成任务 | ❌ | ✅ | ✅ |
| Provider 管理、用户管理、API Key 管理、审计日志查询 | ❌ | ❌ | ✅ |

角色是严格序（viewer < editor < admin），路由级按最低所需角色校验；未知或非法角色一律按低于 viewer 处理（拒绝而非放行）。前端也会按角色隐藏对应入口，但这只是界面友好性处理，真正的权限判断始终在服务端完成。

#### 用户管理操作

在「设置」页「用户管理」卡片中，admin 可以：

- 查看全部用户的用户名、角色、状态（启用中/已禁用）。
- 点击「新建用户」创建账号，指定用户名、初始密码与角色。
- 通过行内下拉框调整已有用户的角色，通过开关启用/禁用账号。
- 点击「重置密码」为用户设置新密码。

系统有一条硬性保护：**不允许把最后一个启用中的 admin 账号禁用或降级**，避免管理员自锁出系统。尝试这样做会被服务端拒绝并给出中文提示。

### 4.2 API Key 生命周期

API Key 用于集成方（如 MCP Server、外部脚本）以 `Authorization: Bearer` 方式调用 KBase API，无需走浏览器 Cookie 会话。

**创建**：设置页「API Key」卡片点击「新建 Key」，填写名称与角色（admin/editor/viewer，决定这把 key 能访问的接口范围）。创建成功后弹出**只显示一次**的完整 key（形如 `kbase_ak_...`），务必立即复制保存——关闭弹窗后将无法再次查看完整 key，之后列表中只显示前缀（用于辨识）。

**使用**：调用方在 HTTP 请求头中加入：

```
Authorization: Bearer kbase_ak_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**吊销**：列表中点击「吊销」，确认后该 key 会被标记为已吊销，Bearer 鉴权通道对该 key 的所有请求立即返回拒绝（软删除，行本身保留以供审计追溯，不能恢复使用）。

数据库中只保存 API Key 的 sha256 哈希与前 8 字符明文前缀，完整密钥不落库，无法通过管理界面或数据库反查。

### 4.3 许可证

#### 状态含义

「设置」页「许可证状态」卡片展示当前许可证状态：

| 状态 | 含义 |
|---|---|
| 试用中（trial） | 未放置 `license.json` 证书文件，默认状态 |
| 有效（valid） | 证书签名验证通过且未过期 |
| 已过期（expired） | 证书签名有效但已超过到期日 |
| 无效（invalid） | 证书文件格式错误、字段缺失，或签名验证失败 |

**重要说明**：许可证状态**仅用于展示**，当前版本不会因为试用/过期/无效而锁定或拦截任何功能，`GET /api/license` 接口的返回结果直接决定这个展示状态。

#### 签发流程（面向实施/交付人员）

许可证使用 Ed25519 非对称签名机制。签发需要仓库外部保管的私钥文件：

```powershell
.venv\Scripts\python scripts\gen_license.py --org "客户名称" --expires 2027-07-06 `
  --private-key D:\Claude Code\kbase-license-private.pem --out license.json
```

- 若 `--private-key` 指向的文件不存在，脚本会**自动生成一对新的 Ed25519 密钥对**，私钥写入该路径，并把对应公钥打印到终端——首次生成后必须手工把这段公钥粘贴进 `kbase/license.py` 的 `_PUBLIC_KEY_B64` 常量，否则后续签发的证书都无法通过校验。
- 私钥文件**必须存放在代码仓库之外**，绝不能提交到版本库；一旦密钥对重新生成，此前所有已签发的证书全部失效，需要重新签发。
- 生成的 `license.json` 默认输出到仓库根目录（该路径已被 gitignore 排除）；也可以通过环境变量 `KBASE_LICENSE_FILE` 指定任意路径，方便部署到客户机器上的固定位置。
- `license.json` 的内容为 `{"org": "客户名称", "expires": "到期日期", "signature": "签名值"}` 三个字段，签名对象是 `{org, expires}` 两个字段的规范化 JSON。

### 4.4 审计日志

「设置」页「用户管理」区域下方（admin 可见）可查询审计日志。系统会自动记录：

- 全部**变更类请求**（如建库、删文档、改配置等），记录动作类型（HTTP 方法 + 路由路径模板）与涉及的资源 ID；
- **登录成功/失败**事件；
- **问答请求**（动作标记为 `query`，仅记录问题的前 100 个字符，避免审计表膨胀）。

每条审计记录包含：时间戳、操作者（用户名或 API Key 名称）、动作、资源标识、详情（JSON，超长内容自动截断）、来源 IP。查询接口为 `GET /api/audit?limit=&offset=`，按时间倒序分页返回，仅 admin 可访问。

> 说明：被角色校验拒绝（403）的请求不会产生审计记录——只有真正被允许执行的操作才会留痕。

### 4.5 Provider 管理

大模型 Provider（供应商/模型接入配置）在设置页「Provider」区域管理，全部为 admin 权限操作：

- **新增**：填写名称、`base_url`（兼容 OpenAI 协议的接口地址）、`api_key_env`（环境变量名，而非密钥本身——密钥永远只存在于运行环境变量中，不落库不落配置文件）、`model`（模型名）、`max_concurrency`（最大并发调用数，默认 4）、可选的 `params`（JSON 格式的透传参数，如 qwen3-32b 关闭思考模式的 `extra_body`）。
- **编辑**：可修改除名称外的字段；名称在编辑态下不可更改。
- **设为默认**：将某个 Provider 设为系统默认（`llm.active`），问答页未显式选择时使用该模型。
- **删除**：当前默认 Provider 不可删除，需先切换默认 Provider。
- **连通性测试**：点击「测试」发起一次 1-token 的探测请求，返回延迟（毫秒）或错误信息，用于快速验证密钥、网络与模型名称是否配置正确。

> **运维提示**：Provider 首次启动时从 `config/kbase.yaml` 种子导入数据库，之后以数据库为准；修改 YAML 配置文件不再对已初始化的部署生效，后续调整一律通过设置页完成。

### 4.6 模型效果评测（选型对比工具）

除了在问答页手动切换模型对比观感，KBase 还提供一个命令行评测工具 `eval/run_eval.py`，用一组固定问答对批量跑多个 Provider，产出检索命中率与答案关键词覆盖率的对比报告，适合在正式上线前决定选用哪个规模/档位的模型：

```powershell
.venv\Scripts\python eval/run_eval.py --kb <知识库id> --providers qwen-plus,qwen-max --out eval/report.md
```

- `--kb`：目标知识库 id（可通过 `GET /api/kb` 查询）。
- `--providers`：逗号分隔的 Provider 名称列表，对比多个模型在同一批问题上的表现。
- `--questions`：问答对文件，默认 `eval/questions.jsonl`（JSONL 格式，每行含 `question` / `expect_doc` / `expect_keywords` 字段），可根据客户实际业务场景自定义评测集。
- 输出的 `report.md` 是评测产物，不会自动提交入库，每次运行会覆盖。

该工具需要在部署机器上通过命令行执行（不在 Web UI 中），通常由实施/交付人员在项目验收阶段或客户希望更换模型档位时使用。

---

## 5. 部署手册

### 5.1 lite 与 standard 选型对照表

| 维度 | lite | standard |
|---|---|---|
| 适用场景 | 演示、POC、小规模验证（单机） | 生产环境、10 并发以上稳定检索场景 |
| 元数据库 | SQLite 单文件 | PostgreSQL 容器 |
| 向量库 | Chroma（嵌入式，进程内） | Qdrant（独立容器） |
| 向量化 | 进程内加载 bge-m3 | 独立 TEI 服务（`tei-embed`） |
| 重排 | 进程内 CrossEncoder | 独立 TEI 服务（`tei-rerank`） |
| 容器数量 | 1 | 5（app / postgres / qdrant / tei-embed / tei-rerank） |
| 硬件要求 | 单机 CPU 即可运行（无 GPU 也可） | 生产建议配 GPU 以获得可用吞吐（见 6.3 容量规划） |
| 首启下载 | bge-m3 + bge-reranker 权重（命名卷持久化） | TEI 容器各自下载 bge-m3 / bge-reranker-v2-m3 权重 |
| 一键部署命令 | `docker compose -f docker-compose.lite.yml up -d --build` | `docker compose -f docker-compose.standard.yml up -d --build` |

两种形态共享同一个应用镜像（`Dockerfile`），区别只是 `config/kbase.*.yaml` 中 embedder/vectorstore/rerank/db 四个插槽指向进程内实现还是外部服务，业务代码完全一致。

### 5.2 lite compose 详解

`docker-compose.lite.yml` 只有一个 `app` 服务：

- 端口映射：容器内 `8100` → 宿主机 `8100`。
- 环境变量：`KBASE_SECRET_KEY`（必需，未设置直接拒绝启动）、`KBASE_ADMIN_PASSWORD`（可选）、`DASHSCOPE_API_KEY`（必需）。
- 数据卷：`./data:/app/data`（SQLite 数据库、Chroma 持久化目录、上传原件）；`hf-cache:/root/.cache/huggingface`（模型权重缓存，避免容器重建后重新下载）。
- 健康检查：每 30 秒访问 `/healthz`，5 次失败视为不健康。

### 5.3 standard compose 详解

`docker-compose.standard.yml` 包含 5 个服务：

| 服务 | 作用 |
|---|---|
| `app` | FastAPI 主应用 |
| `postgres` | 元数据库（PostgreSQL 16），存放知识库/文档/分块/用户/审计等关系数据 |
| `qdrant` | 向量库，独立容器 |
| `tei-embed` | 独立的向量化推理服务（模型 `BAAI/bge-m3`） |
| `tei-rerank` | 独立的重排推理服务（模型 `BAAI/bge-reranker-v2-m3`），默认设置 `--max-concurrent-requests 2048` |

`app` 服务通过 `depends_on` + `condition: service_healthy` 等待四个依赖服务就绪后再启动；`config/kbase.standard.yaml` 以只读 bind-mount 方式挂载覆盖镜像内默认的 `config/kbase.yaml`，因此**只需修改这份 YAML 文件后 `docker compose restart app` 即可生效，无需重新构建镜像**。

**依赖服务地址**：standard 配置里的 endpoint（`http://tei-embed:80`、`http://qdrant:6333`、`postgresql+psycopg://kbase:PASSWORD@postgres:5432/kbase`）均指向 Docker 内置 DNS 解析的服务名，无需手工配置 IP。

**OCR 服务**：MonkeyOCR 运行在 compose 编排范围之外（独立生命周期），通过 `host.docker.internal` 访问宿主机（或宿主机可达）的 OCR 服务地址；Linux 宿主需要在 compose 的 `app` 服务中保留 `extra_hosts: ["host.docker.internal:host-gateway"]` 配置项，Docker Desktop（Windows/Mac）默认已支持无需额外配置。

### 5.4 环境变量全表

| 变量 | 必需/可选 | 说明 |
|---|---|---|
| `KBASE_SECRET_KEY` | **必需** | 会话 JWT 签名密钥；未设置会导致每次重建容器都生成新密钥，所有已登录会话失效 |
| `KBASE_ADMIN_PASSWORD` | 可选 | 首启 admin 初始密码；不设置则随机生成 16 位密码，只打印一次到启动日志 |
| `DASHSCOPE_API_KEY` | **必需** | 大模型网关（阿里云百炼/DashScope）密钥，`config/kbase*.yaml` 中 `api_key_env` 字段指向它 |
| `POSTGRES_PASSWORD` | standard 必需 | PostgreSQL 元数据库密码 |
| `KBASE_API_KEY`（MCP 专用） | 可选 | `kbase_mcp/` 反向调用 KBase API 时使用的 Bearer key；仅当 KBase 已开启鉴权且需要使用 MCP 时才需要 |
| `KBASE_WAIT_FOR` | 内部/可选 | standard compose 自动设置为依赖服务的 `host:port` 列表，entrypoint.sh 据此等待依赖端口就绪；lite 模式或手工部署一般无需关心 |
| `HF_ENDPOINT` | 可选 | 国内网络加速 HuggingFace 模型下载，建议设为 `https://hf-mirror.com` |
| `KBASE_LICENSE_FILE` | 可选 | 自定义 `license.json` 路径，默认仓库根目录 |

### 5.5 TLS 反代要求

**生产部署必须在 KBase 前面挂 TLS（反向代理终结 HTTPS，如 Nginx/Caddy/云负载均衡）。** 原因：登录请求把密码明文放在请求体中，会话 Cookie 默认只设置了 `httponly`/`samesite=lax`（未设置 `Secure` 属性）；裸 HTTP 部署下密码与会话凭证都会在网络上明文传输，存在中间人窃取风险。上生产前需要：

1. 反向代理终结 TLS，KBase 自身继续运行 HTTP（内网/容器间通信）；
2. 在反向代理层给 `kbase_session` Cookie 补加 `Secure` 属性（或在响应头转发时改写），确保浏览器只在 HTTPS 连接上发送该 Cookie。

### 5.6 OCR 服务接入

扫描件/图片文档的识别依赖独立部署的 MonkeyOCR HTTP 服务，通过 `config/kbase*.yaml` 的 `ocr` 配置块接入：

```yaml
ocr:
  enabled: true
  backend: monkey-http
  endpoint: "http://localhost:7861"   # 生产环境请替换为实际 MonkeyOCR 服务地址
```

- `enabled: false` 或服务不可达时，扫描件/图片摄取会优雅降级为「待OCR」状态，不阻塞同批次其他文档，可在 OCR 服务就绪后重试补跑（见 3.3 节）。
- OCR 服务与主系统生命周期独立，可按需启停（如仅在批量导入扫描件时临时启动 GPU 服务器跑 OCR，跑完即可关闭节省成本）。
- 仓库中提交的默认 `endpoint` 仅为演示用途，**生产部署必须替换为客户自己的 MonkeyOCR 服务地址**。

### 5.7 standard GPU 与 CPU 变体

`docker-compose.standard.yml` 中 `tei-embed` / `tei-rerank` 默认使用 CPU 镜像（`ghcr.io/huggingface/text-embeddings-inference:cpu-latest`），任何机器都能跑通，但吞吐远低于 GPU。

生产 GPU 部署需要手工完成两处调整：

1. 把镜像 tag 换成对应 **计算能力（compute capability）** 的 GPU 变体——TEI 按计算能力发布镜像 tag，不是裸版本号。例如 NVIDIA L4（Ada Lovelace 架构，计算能力 8.9）应使用 `89-latest`；Ampere 架构（计算能力 8.0）应使用 `80-latest`。查错架构会直接导致启动失败或静默退化到极低吞吐。
2. 取消注释 `deploy.resources.reservations.devices`（`driver: nvidia`）段落，让容器能够访问 GPU。

GPU 与 CPU 镜像输出结果完全一致，可以先用 CPU 镜像验证功能流程，确认目标机器 GPU 型号后再切换到对应 GPU 镜像。

**国内镜像加速**：`postgres:16-alpine` / `qdrant/qdrant` / TEI 镜像拉取缓慢时，三选一：配置本机 Docker 的 `registry-mirrors`；或把镜像名换成国内加速仓库地址；TEI 启动时从 HuggingFace 下载模型权重，可设置 `HF_ENDPOINT=https://hf-mirror.com`（compose 文件中已给出对应行，默认注释，按需打开）。不要同时改镜像名又配置镜像源。

---

## 6. 运维手册

### 6.1 备份与恢复

#### lite 模式

lite 模式数据全部集中在宿主机挂载目录 `./data`（包含 `kbase.sqlite` 元数据库、`chroma/` 向量数据、`files/` 原始文件），停机复制即可获得一致性快照：

```bash
docker compose -f docker-compose.lite.yml stop app
tar -czf kbase-lite-backup-$(date +%F).tar.gz data/
docker compose -f docker-compose.lite.yml start app
```

**恢复**：停止 app，用备份包解压覆盖 `./data` 目录，再启动 app。

#### standard 模式

standard 模式数据分布在三处，需分别备份：

```bash
# PostgreSQL：pg_dump 逻辑备份（建议 nightly cron）
docker compose -f docker-compose.standard.yml exec -T postgres \
  pg_dump -U kbase kbase | gzip > pg-backup-$(date +%F).sql.gz

# Qdrant：官方 snapshot API，对运行中的实例做一致性快照，无需停机
curl -X POST http://localhost:6333/collections/<collection>/snapshots

# 原始文件卷：直接打包（可在线操作，允许极小的时间窗口不一致，摄取中的文档下次重试即可）
tar -czf kbase-files-backup-$(date +%F).tar.gz data/files
```

**恢复**：

- PostgreSQL：`gunzip -c pg-backup-*.sql.gz | docker compose exec -T postgres psql -U kbase kbase`
- Qdrant：使用其 snapshot 恢复 API（`PUT /collections/<collection>/snapshots/recover`，指向快照文件）
- 文件卷：解压覆盖 `data/files` 即可

**cron 示例**（每天凌晨 3 点执行 PostgreSQL 备份）：

```
0 3 * * * cd /path/to/kbase-standard && docker compose -f docker-compose.standard.yml exec -T postgres pg_dump -U kbase kbase | gzip > /backups/pg-$(date +\%F).sql.gz
```

#### 重建索引（不重新解析原始文件）

如果需要为已入库文档补建关键词索引、或在调整了知识库级增强配置后重新生成向量，可使用：

```powershell
.venv\Scripts\python -m kbase.reindex --kb <知识库id>
```

该命令基于摄取时已保存的 Markdown 中间产物重新分块、建索引，**不会重新解析原始文件**（原始文件仍完整保留在 `data/files` 下，双存机制保证重建索引不需要重新走一遍 OCR/markitdown 解析）。适用场景：调整分块策略后希望旧文档也受益、FTS 索引因异常损坏需要重建等。

### 6.2 监控

`GET /healthz`（无需鉴权）返回系统健康状态，可用于容器健康检查与外部监控探针：

```json
{
  "status": "ok",
  "embedder": "TEIEmbedder",
  "vectorstore": "QdrantStore",
  "reranker": "on",
  "rerank_stats": {
    "rerank_total": 0,
    "rerank_shed_load_total": 0,
    "rerank_error_total": 0
  }
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `status` | 固定 `"ok"`（进程存活即返回，不代表全部依赖都健康） |
| `embedder` / `vectorstore` | 当前加载的插件实现类名，用于确认部署形态（lite/standard）是否符合预期 |
| `reranker` | `"on"`（正常重排中）/ `"degraded"`（发生过载自适应降级）/ `"off"`（未启用重排） |
| `rerank_stats.rerank_total` | 累计重排调用次数 |
| `rerank_stats.rerank_shed_load_total` | 因重排并发已满、主动跳过重排改用融合排序的累计次数（过载自适应降级，详见 6.3） |
| `rerank_stats.rerank_error_total` | 重排调用异常（非过载导致）的累计次数 |

建议将 `rerank_shed_load_total` 相对 `rerank_total` 的比例（降级率）纳入日常监控，持续高降级率意味着当前并发已超出重排服务的舒适区，用户拿到的是不重排的融合排序结果，检索质量会低于全精排。

### 6.3 容量规划

以下数据来自 standard 部署形态在参考硬件上的真实压测（GCP `g2-standard-4`：4 vCPU、NVIDIA L4 单卡，与常驻 MonkeyOCR 共存），压测目标为 `POST /api/kb/{id}/search`（含向量化 + 双路检索 + RRF 融合 + 重排，不含大模型生成环节）。数据如实记录，包含未达标部分。

| 并发 | P50 | P95 | 说明 |
|---|---|---|---|
| 10 | 约 110~800ms | **约 860ms** | 全精排舒适区，几乎不触发降级 |
| 50 | 约 1.6~4.1s | 约 2.5~4.3s | 约 63% 请求触发过载降级 |
| 100 | 约 3.3~8.3s | **约 4.2s（优化后）** | 约 63% 请求触发过载降级（自适应跳过重排，改用融合排序，不是错误） |

**关键结论**：

- **全精排舒适区大致在 10 并发以内**：该并发量级下延迟接近但尚未完全达到验收线（500ms），具体取决于瞬时负载。
- **100 并发依赖自适应降级支撑**：`retrieval.rerank.max_concurrency`（默认 8）限制同时在途的重排调用数，超出的查询不排队等待重排 GPU，而是直接降级为融合排序（跳过重排但仍是真实的稠密+关键词双路检索结果，带引用，不是空结果或错误）。
- **100 并发 P95 实测约 4.2 秒**，是 500ms 验收线的约 8 倍，**未达标**。
- **瓶颈是重排推理算力，不是显存**：实测显存占用远低于总容量上限，问题在于单卡 GPU 的计算吞吐与调度排队；扩大显存或调整线程池容量均未见改善（线程池从 40 调到 120 后 4 vCPU 参考机上反而回退约 20%，根因是物理核心数而非线程槽位数）。
- **要在 100 并发下把全精排 P95 压到 500ms 以内，需要独立的重排 GPU 或更强算力卡**，或者接受高并发下自动降级为融合排序（不重排）的产品取舍。

**扩容路径建议**（按投入产出排序）：

1. 优先评估实际业务并发是否真的会持续接近 100——真实流量的请求到达模式通常比压测的"无思考时间连续轰炸"更稀疏，实际降级率会低于压测数字。
2. 为重排单独配置 GPU 实例（不与向量化、OCR 共享），是提升重排吞吐最直接的方式。
3. 减少每次检索的候选数（`retrieval.candidates`，默认 20）可降低重排负载，但会牺牲召回质量，需要业务权衡。
4. 不建议单纯调大 `server.threadpool_size` 或 `retrieval.rerank.max_concurrency` 期望获得性能提升——已通过压测证伪，在 vCPU 数量有限的机器上调大线程池反而可能因调度开销增大而性能回退。

完整压测方法论、逐档数据与推导过程见仓库内 `loadtest/report-standard.md`。

### 6.4 lite → standard 迁移

当前**没有自动化迁移工具**。lite 使用的 SQLite/Chroma 与 standard 使用的 PostgreSQL/Qdrant 不是同一套 schema 与索引结构，字节级搬迁不现实。迁移思路是**原件重摄取**：

1. 从 lite 环境的 `data/files/` 目录取出所有原始文档（文件名与 `GET /api/kb/{id}/documents` 返回的 `filename` 对应）。
2. 在 standard 栈上创建同名知识库，将原件逐个重新上传。
3. 等待摄取完成（状态变为"就绪"），核对文档数量与 lite 侧一致。
4. 会话历史、审计日志等运行时数据不在本迁移范围内，如需保留需另行导出 lite 侧 SQLite 对应表。

### 6.5 故障排查表

| 现象 | 可能原因 | 处理方式 |
|---|---|---|
| 文档长期停留在"待OCR"状态 | OCR（MonkeyOCR）服务未启动、地址配置错误，或服务临时不可达 | 检查 `config/kbase*.yaml` 的 `ocr.endpoint` 是否指向正确且可达的服务地址；服务恢复后点击「批量重试OCR」或对单个文档点击重试 |
| 登录返回 401 | 用户名/密码错误、账号被禁用，或会话 Cookie 因 `KBASE_SECRET_KEY` 变化而失效 | 确认账号状态；检查部署是否稳定设置了 `KBASE_SECRET_KEY`（未设置会在每次重建容器后使旧会话全部失效） |
| 检索结果质量下降、明显没有走重排 | 高并发下触发了重排过载自适应降级 | 查看 `/healthz` 的 `reranker` 字段是否为 `"degraded"`，对比 `rerank_stats` 中 `rerank_shed_load_total` 与 `rerank_total` 的比例；属于设计内行为，见 6.3 节容量规划 |
| standard 部署下检索响应变慢但无报错 | 同上，或 TEI-rerank / Qdrant 服务本身压力较大 | 检查 `docker compose logs tei-rerank`；确认当前并发是否超出参考硬件的舒适区（约 10 并发） |
| 上传旧版 `.doc` 文件失败或解析结果异常 | `.doc` 是微软旧版二进制格式，`markitdown` 依赖的解析库不支持 | 用 Microsoft Word 或 WPS 另存为 `.docx` 后重新上传 |
| 大模型调用超时或连接异常 | 网络问题、API Key 无效、DashScope 服务端限流 | 在设置页对该 Provider 执行「测试」连通性检测，查看返回的延迟或错误信息；确认 `DASHSCOPE_API_KEY` 环境变量已正确注入容器 |
| standard 部署 GPU 版 TEI 容器无法启动或吞吐极低 | GPU 镜像 tag 与实际显卡计算能力（compute capability）不匹配 | 核实显卡型号对应的计算能力，替换为正确的镜像 tag（如 L4 用 `89-latest`），参见 5.7 节 |
| 镜像/模型下载缓慢 | 未配置国内镜像加速 | 参考 2.3 与 5.7 节配置 `HF_ENDPOINT` 或 Docker `registry-mirrors` |

---

## 7. 集成手册

### 7.1 REST API 概览

#### 鉴权方式

除 `POST /api/auth/login`、`GET /healthz` 及静态资源外，全部接口都需要鉴权，支持两种通道：

- **Cookie 会话**：浏览器登录后自动携带 `kbase_session` Cookie（HttpOnly，SameSite=Lax，有效期 7 天）。
- **API Key（Bearer）**：适合脚本/集成方调用，请求头携带 `Authorization: Bearer kbase_ak_...`，在设置页「API Key」卡片创建（见 4.2 节）。

角色最低要求按 `viewer < editor < admin` 序，未达到最低角色要求返回 403。

#### 主要端点表

| 方法 | 路径 | 最低角色 | 说明 |
|---|---|---|---|
| POST | `/api/auth/login` | 无需鉴权 | `{username, password}` → 设置会话 Cookie，返回 `{username, role}` |
| POST | `/api/auth/logout` | viewer | 清除会话 Cookie |
| GET | `/api/auth/me` | viewer | 返回当前登录用户 `{username, role}` |
| GET | `/api/kb` | viewer | 列出知识库 |
| POST | `/api/kb` | editor | 建知识库，`{name}` |
| DELETE | `/api/kb/{kb_id}` | editor | 级联删除知识库 |
| PUT | `/api/kb/{kb_id}/config` | editor | 更新分块/增强配置 |
| GET | `/api/kb/{kb_id}/documents` | viewer | 文档列表与状态 |
| POST | `/api/kb/{kb_id}/documents` | editor | 批量上传文档（multipart） |
| GET | `/api/documents/{doc_id}/content` | viewer | 获取文档 Markdown 全文 |
| DELETE | `/api/kb/{kb_id}/documents/{doc_id}` | editor | 删除文档 |
| POST | `/api/documents/{doc_id}/retry` | editor | 重试单个文档解析 |
| POST | `/api/kb/{kb_id}/retry-ocr` | editor | 批量重试该知识库全部待OCR文档 |
| POST | `/api/kb/{kb_id}/query` | viewer | 检索问答（SSE 流式），`{question, provider?, top_k=5}` |
| POST | `/api/kb/{kb_id}/search` | viewer | 纯检索，`{query, top_k=5, debug=false}` |
| POST | `/api/conversations` | editor | 创建会话，`{kb_id}` |
| GET | `/api/conversations` | viewer | 会话列表（分页） |
| GET | `/api/conversations/{conv_id}/messages` | viewer | 会话消息历史 |
| POST | `/api/conversations/{conv_id}/query` | viewer | 多轮会话问答（SSE，含查询改写） |
| POST | `/api/proposals/outline` | editor | 同步生成方案大纲 |
| POST | `/api/jobs` | editor | 创建生成任务（`type: proposal\|digest`） |
| GET | `/api/jobs?kb_id=` | viewer | 列出该知识库的任务 |
| GET | `/api/jobs/{id}` | viewer | 查询任务详情/进度 |
| GET | `/api/jobs/{id}/artifact?format=md\|docx` | viewer | 下载任务产物 |
| GET | `/api/providers` | viewer | 列出可用 Provider 名称 |
| GET/POST/PUT/DELETE | `/api/settings/providers*` | admin | Provider 增删改查 |
| PUT | `/api/settings/active-provider` | admin | 设置默认 Provider |
| POST | `/api/settings/providers/{name}/test` | admin | Provider 连通性测试 |
| GET/POST | `/api/settings/api-keys` | admin | API Key 列表/创建 |
| DELETE | `/api/settings/api-keys/{key_id}` | admin | 吊销 API Key |
| GET/POST/PUT | `/api/users*` | admin | 用户管理 |
| GET | `/api/audit?limit=&offset=` | admin | 审计日志查询 |
| GET | `/api/license` | viewer | 许可证状态 |
| GET | `/healthz` | 无需鉴权 | 系统健康状态 |

> 完整字段细节以 OpenAPI 文档为准；`auth="on"`（生产默认）时 `/docs`/`/redoc`/`/openapi.json` 出于安全考虑被关闭。

### 7.2 MCP 接入步骤

MCP（Model Context Protocol）Server 把知识库能力暴露为标准工具，供 Claude Code / Claude Desktop 等支持 MCP 的客户端直接调用。它通过 HTTP 反向调用运行中的 KBase API，**不会**额外加载一份模型内核，因此**必须先启动 KBase API 服务**才能使用。

#### 三个工具

| 工具 | 参数 | 返回 |
|---|---|---|
| `list_knowledge_bases()` | 无 | 全部知识库列表 `[{id, name}, ...]` |
| `search_knowledge(kb_id, query, top_k=5)` | 纯检索 | 带出处与相关度的原文块列表（不生成答案） |
| `ask_knowledge_base(kb_id, question, provider=None)` | 完整问答 | `{answer, citations: [...]}`（内部消费 SSE 流后一次性返回完整结果，不流式） |

**错误契约**：任一工具失败时（KBase 未启动、知识库不存在、密钥缺失等）不会抛出协议级异常，而是返回 `{"error": "<中文说明>"}`——调用方应先检查返回对象是否包含 `error` 键。

#### 安装

```powershell
.venv\Scripts\python -m pip install -e ".[mcp]"
```

#### 启动

```powershell
# STDIO（默认）——供 Claude Code / Claude Desktop 这类以子进程方式拉起的客户端使用
.venv\Scripts\python -m kbase_mcp

# Streamable HTTP——供远程/多客户端场景使用
.venv\Scripts\python -m kbase_mcp --http --port 3001 --host 127.0.0.1
```

#### 环境变量

| 变量 | 说明 |
|---|---|
| `KBASE_API_URL` | MCP 反向调用的 KBase API 地址，默认 `http://localhost:8100` |
| `KBASE_MCP_TOKEN` | 仅影响 HTTP 传输的鉴权；不设置则不校验（适合本机/内网可信环境）；设置后 HTTP 请求必须带 `Authorization: Bearer <token>`，否则 401。STDIO 传输不受影响 |
| `KBASE_API_KEY` | 若 KBase API 已开启鉴权（生产默认），MCP 反调 KBase 的每个请求都需要带凭据；在设置页「API Key」卡片创建后填入此变量。角色按用途选择：纯问答/检索给 viewer 即可，需要建库/上传的自动化流程给 editor |

#### 注册到 Claude Code

```powershell
claude mcp add kbase -- python -m kbase_mcp
```

#### 注册到 Claude Desktop

在 `claude_desktop_config.json` 中加入：

```json
{
  "mcpServers": {
    "kbase": {
      "command": "python",
      "args": ["-m", "kbase_mcp"],
      "env": {
        "KBASE_API_URL": "http://localhost:8100"
      }
    }
  }
}
```

### 7.3 SSE 事件格式

问答接口（`POST /api/kb/{id}/query` 与 `POST /api/conversations/{id}/query`）均以 SSE（Server-Sent Events）方式返回，事件序列固定为：

```
event: citations
data: [{"index":1,"doc_id":"...","doc_name":"...","heading_path":"...","snippet":"...","score":0.82}, ...]

event: token
data: 这是

event: token
data: 回答的第一段文字……

event: done
data:
```

字段说明：

- **`citations`** 事件最先返回，`data` 是该次回答所有可用引用的 JSON 数组（拒答场景下为空数组 `[]`）。引用编号（`index`）与回答正文中出现的 `[n]` 角标一一对应。
- **`token`** 事件随生成过程逐段返回，`data` 是文本片段，客户端应将多个 `token` 事件的内容顺序拼接得到完整回答。
- **`done`** 事件标志一次问答正常结束，`data` 为空字符串。

客户端实现建议：以 `event:` 字段区分事件类型分别处理；网络中断导致提前结束时（未收到 `done`）应向用户提示"回答中断，请重试"，而不是当作已完整完成。

---

## 8. 附录

### 8.1 配置参考（`config/kbase.yaml` 全字段表）

以下字段以 `kbase/config.py` 中的 Pydantic 模型定义为准，字段名、默认值、类型均与代码一致。

#### 顶层 `AppConfig`

| 字段 | 默认值 | 说明 |
|---|---|---|
| `data_dir` | `./data` | 元数据库/向量库/原始文件与 Markdown 存放目录 |

#### `db`（元数据库）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `db.url` | `sqlite:///{data_dir}/kbase.sqlite` | 数据库连接串；`{data_dir}` 占位符会被实际路径替换；`postgresql+psycopg://...` 等其他 URL 原样透传 |

#### `embedder`（向量化）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `embedder.name` | `bge-local` | 向量化插件名（`bge-local` 进程内加载 / `tei` 调用外部 TEI 服务） |
| `embedder.model` | `BAAI/bge-m3` | 模型名 |
| `embedder.endpoint` | `null` | `name="tei"` 时必填：TEI 服务地址 |

#### `vectorstore`（向量库）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `vectorstore.name` | `chroma` | 向量库插件名（`chroma` 嵌入式 / `qdrant` 独立服务） |
| `vectorstore.endpoint` | `null` | `name="qdrant"` 时必填：Qdrant 服务地址 |
| `vectorstore.api_key` | `null` | Qdrant Cloud 等需要鉴权的部署可选填 |

#### `chunker`（分块）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `chunker.name` | `structure` | 分块策略名（结构分块：按标题层级 + 父子块） |
| `chunker.chunk_size` | `512` | 叶子块大小（字符数） |
| `chunker.chunk_overlap` | `64` | 相邻块重叠字符数 |

#### `llm`（大模型 Provider）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `llm.active` | 必填，无默认 | 默认使用的 Provider 名称，必须存在于 `providers` 列表中 |
| `llm.providers[].name` | 必填 | Provider 唯一标识 |
| `llm.providers[].base_url` | 必填 | OpenAI 兼容接口地址 |
| `llm.providers[].api_key_env` | 必填 | 密钥所在环境变量名（密钥本身不进配置文件） |
| `llm.providers[].model` | 必填 | 实际模型名 |
| `llm.providers[].max_concurrency` | `4` | 该 Provider 最大并发调用数 |
| `llm.providers[].params` | `{}` | 每次调用透传给 `chat.completions.create` 的默认参数（如 `extra_body`） |

#### `retrieval`（检索）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `retrieval.hybrid` | `true` | 是否启用混合检索（向量 + 关键词双路） |
| `retrieval.candidates` | `20` | 每路召回数与融合候选数 |
| `retrieval.rrf_k` | `60` | RRF 融合算法的 k 常数 |
| `retrieval.min_score_dense` | `0.3` | 未启用重排时的拒答阈值（余弦相似度） |
| `retrieval.min_score_rerank` | `0.35` | 启用重排时的拒答阈值（重排分数） |
| `retrieval.min_include_score` | `0.1` | 收录底线：过了拒答门后，高于此分数的块都会被收录（保留冲突/佐证证据） |
| `retrieval.max_parent_chars` | `4000` | 父块截窗上限字符数，避免单块过大撑爆生成上下文 |

##### `retrieval.rerank`（重排子配置）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `retrieval.rerank.enabled` | `true` | 是否启用重排 |
| `retrieval.rerank.name` | `bge-local` | 重排插件名（`bge-local` 进程内 / `tei` 调用外部 TEI 服务） |
| `retrieval.rerank.model` | `BAAI/bge-reranker-v2-m3` | 重排模型名 |
| `retrieval.rerank.endpoint` | `null` | `name="tei"` 时必填：TEI 服务地址 |
| `retrieval.rerank.max_concurrency` | `8` | 同时在途的重排调用数上限；超出的查询非阻塞跳过重排，直接降级为融合排序 |

##### `retrieval.rewrite`（查询改写子配置）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `retrieval.rewrite.mode` | `conditional` | `off`（从不改写）/ `conditional`（启发式触发，默认）/ `always`（有历史即触发） |
| `retrieval.rewrite.provider` | `null` | 改写调用使用的 Provider；不填则用 `llm.active` |
| `retrieval.rewrite.max_wait_s` | `5.0` | 改写调用超时时间（秒），超时/失败静默回退为原问题 |

#### `enrich`（上下文增强）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `enrich.provider` | `null` | 增强调用使用的 Provider；不填则用 `llm.active`；是否真正增强由每个知识库自己的配置决定 |

#### `ingest`（摄取）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `ingest.workers` | `2` | 批量上传时并行摄取的线程数 |

#### `ocr`（OCR）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `ocr.enabled` | `false` | 是否启用 OCR；关闭时扫描件/图片直接判失败 |
| `ocr.backend` | `monkey-http` | OCR 后端实现（目前仅此一种） |
| `ocr.endpoint` | `http://localhost:7861` | MonkeyOCR 服务地址 |

#### `server`（服务器）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `server.threadpool_size` | `40` | AnyIO/Starlette 线程池容量，检索等同步操作经此线程池执行；仅当部署机 vCPU 充裕（≥16）且经压测验证有收益时才建议调大，否则保持默认 |

> **注意**：`rewrite.mode` 配置为字符串时必须加引号（如 `mode: "off"`），否则 `off` 会被 YAML 解析为布尔值 `False` 而在启动时报校验错误。

### 8.2 FAQ

**Q：许可证过期或无效会影响系统使用吗？**
A：不会。当前版本的许可证机制仅用于状态展示（试用/有效/过期/无效），不会锁定或拦截任何功能。

**Q：为什么不支持旧版 `.doc` 格式？**
A：`.doc` 是微软的旧版专有二进制格式，系统摄取依赖的 `markitdown` 解析库面向现代 Office Open XML 格式（`.docx`）设计。请使用 Word 或 WPS 将文件另存为 `.docx` 后重新上传。

**Q：修改 `config/kbase.yaml` 里的 Provider 配置为什么没有生效？**
A：Provider 配置只在系统首次启动、数据库为空表时从 YAML 种子导入，之后以数据库为准。请通过设置页的 Provider 管理界面进行后续调整。

**Q：知识库配置（分块大小等）修改后，已上传的文档需要重新处理吗？**
A：配置修改只影响后续新上传的文档，不会自动对已入库文档重新分块/生效。如需让已有文档应用新配置，需要删除后重新上传，或使用 `python -m kbase.reindex --kb <id>` 重建索引（不会重新解析原始文件，基于已保存的 Markdown 中间产物）。

**Q：为什么高并发下问答/检索感觉"重排失效了"？**
A：这是系统的过载自适应降级设计（见 6.3 节），当同时在途的重排请求数超过 `retrieval.rerank.max_concurrency`（默认 8）时，超额请求会主动跳过重排、直接使用融合排序结果，而不是排队等待。可通过 `/healthz` 的 `reranker`/`rerank_stats` 字段观测是否发生降级。

**Q：MCP 工具调用返回 `{"error": "..."}` 是什么意思？**
A：这是 KBase MCP 的错误契约设计——工具失败时不会抛出协议级异常，而是返回一个包含 `error` 键的普通结果对象，调用方（如 Claude Code）应先检查返回值中是否有 `error` 字段再消费其余字段。常见错误包括 KBase API 未启动、知识库 ID 不存在、API Key 未配置或已失效等，错误文本本身是中文可读说明。

**Q：lite 模式可以直接升级到 standard 模式吗？**
A：目前没有自动化迁移工具，需要手工将原始文档重新上传到 standard 栈重新摄取（详见 6.4 节）。会话历史等运行时数据不在迁移范围内。

**Q：忘记了首启管理员密码怎么办？**
A：如果设置过 `KBASE_ADMIN_PASSWORD` 环境变量，可以在其他终端登录后到用户管理页重置；如果依赖随机生成密码且日志已丢失，需要直接操作数据库重置该用户的密码哈希，或联系技术支持协助处理。

**Q：审计日志会记录问题的完整内容吗？**
A：不会。问答类审计记录只保留问题的前 100 个字符，用于留痕"谁在什么时候问了什么类型的问题"，不是完整对话记录的存档（完整对话内容保存在会话消息表中，非审计日志）。
