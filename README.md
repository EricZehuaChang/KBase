# KBase 私有化知识库系统

KBase 是一套私有化交付的 B 端知识库系统：摄取（markitdown 解析）→ 分块（父子分块+结构分块）→ 索引（向量库）→ 检索 → 生成（带引用溯源），全链路插件化（Embedder / VectorStore / LLMProvider / Chunker 均为可替换插槽），支持容器化私有化部署。当前代码为 **M1 / lite 演示形态**：单实例、SQLite + Chroma 嵌入式 + 进程内 bge-m3，用于快速验证 RAG 骨架与演示；生产形态（standard profile）见架构文档。

## 快速开始（lite 模式）

```powershell
# 1. 创建虚拟环境并安装依赖（含本地向量化 extra）
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,local-embed]"

# 2. （可选）配置 HuggingFace 镜像，加速 bge-m3 模型下载
$env:HF_ENDPOINT = "https://hf-mirror.com"
# 注：若 hf-mirror 因 308 重定向下载失败，去掉该环境变量改为直连官方源即可

# 3. 加载 .env 中的密钥（DASHSCOPE_API_KEY 等，.env 本身已 gitignore）
Get-Content -Encoding utf8 .env | ForEach-Object {
    if ($_ -match '^([A-Z_0-9]+)=(.+)$') {
        Set-Item -Path "env:$($Matches[1])" -Value $Matches[2]
    }
}

# 4. 启动服务（首次启动需加载 bge-m3 模型，约 60~120 秒）
.venv\Scripts\uvicorn --factory kbase.api.main:create_app --port 8100
```

浏览器打开 http://localhost:8100 即可使用知识库管理与问答页面。`/healthz` 可查看各插件加载状态。

## 配置

运行配置在 `config/kbase.yaml`：

```yaml
data_dir: ./data          # 元数据库/向量库/原始文件与 Markdown 存放目录
embedder:
  name: bge-local          # 向量化插件名（注册表按 name 分派实现）
  model: BAAI/bge-m3
vectorstore:
  name: chroma             # lite 用嵌入式 Chroma；生产可换 Qdrant 适配器
chunker:
  name: structure           # 结构分块（标题层级 + 父子块）
  chunk_size: 512
  chunk_overlap: 64
llm:
  active: qwen-plus         # 默认 provider，前端下拉可按会话切换
  providers:
    - name: qwen-plus
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_key_env: DASHSCOPE_API_KEY   # 密钥永远走环境变量，配置文件本身可入库
      model: qwen-plus
      max_concurrency: 4
    # 可继续追加 qwen-max / deepseek-v3 等 OpenAI 兼容端点
```

要点：

- **密钥不落盘配置**：每个 provider 只声明 `api_key_env`（环境变量名），实际密钥由 `.env`（gitignored）在启动前注入到进程环境。
- **provider 可运行时切换**：查询接口 `POST /api/kb/{id}/query` 接受 `provider` 字段，不传则用 `llm.active`；前端问答页有下拉框，方便同一问题对比不同规模模型的回答。

> **运维提示**：providers 首次启动时从 `config/kbase.yaml` 种子导入数据库，之后以数据库为准（设置页管理），修改 YAML 不再生效。

### 多轮查询改写（QueryRewrite）

会话问答中，像"那司局级呢？"这类依赖上一轮上下文的追问，字面检索往往召回不到相关内容而被拒答。`retrieval.rewrite` 在检索前用 LLM 把这类追问改写为语义完整、可独立检索的问题，仅影响检索这一步——生成回答与会话落库仍固定使用用户原文，界面上看到的问题和历史记录不受影响。

```yaml
retrieval:
  rewrite:
    mode: conditional   # off | conditional | always，默认 conditional
    provider: null      # 不填则用 llm.active 对应的 provider 做改写调用
    max_wait_s: 5.0
```

三态说明：

- `off`：从不改写，检索直接用用户原文（等价于 M2 行为）。
- `conditional`（默认）：启发式判断触发——追问较短、含指代词（那/这/它/该……），或与历史几乎无字符重叠（疑似省略主语的换话题追问）才改写；自包含的完整问题不会被误改写。
- `always`：只要会话已有历史就改写（即使问题本身已经完整），适合对话轮次普遍简短的场景。

改写失败（LLM 超时/报错/空输出）一律静默回退为用户原文，不阻塞主查询链路；触发改写的详细文本会以 `INFO` 级别记录在 `kbase.rag.rewriter` logger（`原文 → 改写后`），便于排查召回质量问题时确认改写是否生效、改写结果是否合理。

## 前端开发

前端（`web-app/`）是独立的 Vite + Vue3 + TypeScript + shadcn-vue 项目，构建产物输出到 `web/`，由 FastAPI 静态托管（`create_app` 的 `web_dir` 逻辑不变）。

```powershell
# 开发模式：先启动后端（见上方“快速开始”，端口 8100），另开一个终端：
cd web-app
npm install
npm run dev          # dev server 默认 5173，vite.config.ts 已把 /api 与 /healthz 代理到 http://localhost:8100

# 构建：产物写入 ../web（即仓库根目录的 web/），供 FastAPI 直接托管
npm run build

# 测试（vitest，纯函数 + 组合式函数单测，无需启动后端）
npm run test
```

> **`web/` 是构建产物，不要直接编辑**：修改前端一律改 `web-app/src/` 下的源码，然后 `npm run build` 重新生成 `web/`，再把 `web/` 的变更一并提交入库（私有化交付环境通常不装 Node，因此构建产物需要入库，而不是靠 `.gitignore` 排除）。直接手改 `web/` 里的文件会在下次构建时被覆盖丢失。

### 四页功能简介

- **问答**（`/`）：左侧会话列表（按更新时间分组：今天/7 天内/更早）+ 右侧流式问答区，回答中的引用角标 `[n]` 可点击打开引用抽屉（片段高亮、相关度分数、跳转全文预览）。
- **知识库管理**（`/kb`）：知识库卡片网格 + 文档表格（状态徽章、失败重试、批量重试 OCR、拖拽上传、分块参数配置）。
- **检索分析**（`/analysis`）：输入查询后展示稠密路/关键词路/RRF 融合三栏对比，若启用重排还会显示重排后名次变化。
- **设置**（`/settings`）：LLM Provider 卡片管理（增删改、设为默认、连通性测试）、系统健康面板、亮/暗主题切换。

### 主题定制

配色、圆角、阴影等设计令牌全部集中在 `web-app/src/styles/tokens.css`，以 CSS 变量形式定义（`:root` 为亮色，`[data-theme="dark"]` 覆盖为暗色）。定制主题（例如行业配色）只需覆盖 `--accent` / `--accent-weak` / `--accent-text` 这一组强调色变量，其余中性色与语义色（`--ok`/`--warn`/`--err`）建议保持不变以维持可读性对比度；shadcn-vue 组件通过 `main.css` 里的映射（`--primary: var(--accent)` 等）统一吃同一套令牌，无需单独改组件样式。

## 生成

除逐轮问答外，KBase 还支持两类"批量生成"场景，均以 job 后台任务的形式运行（提交即返回 `job_id`，前端/客户端轮询进度），产物落盘为 Markdown，并可按需导出 docx。前端「生成」页（导航第 5 项）提供完整向导 UI；下面同时给出 API 直调方式。

### 场景一：方案生成（大纲 → 逐节生成 → 引用汇整）

1. **生成大纲**：`POST /api/proposals/outline` `{kb_id, topic, requirements, provider?}`，同步返回 3~7 节的大纲（`[{title, brief}, ...]`），前端可编辑（增删节、改标题/要点、调整顺序）后再提交。
2. **建 proposal job**：`POST /api/jobs` `{type: "proposal", kb_id, provider?, params: {topic, requirements, outline}}`，返回 `{id}` 后立即在后台执行——大纲每一节单独检索（`topic + 节标题 + brief` top-5）并调用 LLM 生成正文，某节检索不到可用依据时不调用 LLM、直接写入"知识库中无相关依据，本节未生成"占位（不编造），不影响其余节继续生成。
3. 全部节生成完成后汇整：按 `(文档名, 标题路径)` 对所有节的引用做全局去重编号，正文中的 `[n]` 重映射为全局编号，文末追加 `## 引用文献` 附录列出编号对应的文档出处。
4. **轮询进度**：`GET /api/jobs/{id}`，`progress.steps` 是逐步更新的数组（每节一步 + 汇整一步 + 写产物一步），每步 `status` 为 `pending|running|done|failed`；单节失败不影响整体，全部结束后 job 顶层 `status` 为 `done`（无失败步）或 `done_with_errors`（有步骤失败但仍有产出）。
5. **下载产物**：`GET /api/jobs/{id}/artifact?format=md|docx`（job 未到终态返回 409）。

### 场景二：定期汇编（多文档摘要）

`POST /api/jobs` `{type: "digest", kb_id, provider?, params: {doc_ids?}}`——`doc_ids` 省略则汇总该知识库全部 `ready` 状态文档。每个文档单独一步：读取其 `content.md` 前 6000 字交给 LLM 生成 200 字以内摘要；所有文档摘要完成后再请求一段总览。产物结构固定为 `# {知识库名}文档汇编` + `## 总览` + 每文档一个 `## 文件名` 段落。轮询与下载方式与方案生成一致（`GET /api/jobs/{id}`、`GET /api/jobs/{id}/artifact`）。

### API 端点简表

| 方法/路径 | 说明 |
|---|---|
| `POST /api/proposals/outline` | 同步生成方案大纲（`{kb_id, topic, requirements, provider?}` → 大纲数组） |
| `POST /api/jobs` | 建 job（`type: proposal\|digest`），立即返回 `{id}`，后台执行 |
| `GET /api/jobs?kb_id=` | 列出该知识库的 job（按更新时间倒序） |
| `GET /api/jobs/{id}` | 查询 job 详情（`status`/`progress`/`artifact_path`/`error`） |
| `GET /api/jobs/{id}/artifact?format=md\|docx` | 下载产物（job 未到终态 409；产物不存在 404） |

### docx 导出说明

`format=docx` 首次请求时才按需从已生成的 `artifact.md` 转换并缓存（同一 job 后续请求直接复用缓存文件，不重复转换）。转换规则（`kbase/jobs/export_docx.py`）：Markdown `#`/`##`/`###` → Word Heading 1~3，空行分段，`- ` 列表项，`**加粗**` 转为加粗 run，其余文本按普通段落处理。依赖 `python-docx`（已在 `pyproject.toml` 主依赖组，`pip install -e ".[dev,local-embed]"` 时随主依赖一并安装，无需额外 extra）。

## 测试

```powershell
# 快速套件（默认排除需要网络/大模型下载的用例）
.venv\Scripts\python -m pytest

# 完整套件（含真实 API 调用、bge-m3 真实推理等，需要网络与有效 API Key）
.venv\Scripts\python -m pytest -m external -v
```

## 评测（模型对比）

`eval/run_eval.py` 跑一组问答对，产出检索命中率 + 答案关键词覆盖率的多 provider 对比报告：

```powershell
.venv\Scripts\python eval/run_eval.py --kb <kb_id> --providers qwen-plus,qwen-max --out eval/report.md
```

- `--kb` 为目标知识库 id（`GET /api/kb` 可查）
- `--providers` 逗号分隔，对比多个模型在同一批问题上的表现
- `--questions` 默认 `eval/questions.jsonl`（JSONL，每行 `question` / `expect_doc` / `expect_keywords`）
- 输出的 `report.md` 是生成产物，已在 `.gitignore` 中排除（`eval/report*.md`），不会入库

## MCP Server

`kbase_mcp/` 是一个独立的 MCP（Model Context Protocol）进程，把知识库暴露为三个工具（`list_knowledge_bases` / `search_knowledge` / `ask_knowledge_base`），供 Claude Code / Claude Desktop 等 MCP 客户端直接调用。它通过 HTTP 反调运行中的 KBase API，**不会**再加载一份模型内核——因此**必须先启动 KBase API**（见上方"快速开始"，默认 `http://localhost:8100`），再启动 MCP Server。

> **工具错误契约**：任一工具在失败时（KBase 未启动、kb_id 不存在、provider 密钥缺失等）不抛协议级异常，而是正常返回 `{"error": "<中文说明>"}`——客户端应先检查返回对象是否含 `error` 键再消费业务字段。

### 安装

```powershell
.venv\Scripts\python -m pip install -e ".[mcp]"
```

### 启动（两种传输）

```powershell
# STDIO（默认）——供 Claude Code / Claude Desktop 这类以子进程方式拉起的客户端使用
.venv\Scripts\python -m kbase_mcp

# Streamable HTTP——供远程/多客户端场景使用
.venv\Scripts\python -m kbase_mcp --http --port 3001 --host 127.0.0.1
```

环境变量：

- `KBASE_API_URL`：MCP 反调的 KBase API 地址，默认 `http://localhost:8100`。
- `KBASE_MCP_TOKEN`：仅影响 HTTP 传输的鉴权。**不设置**＝鉴权关闭（任何请求都放行，适合本机/内网可信环境）；**设置后**，HTTP 请求必须带 `Authorization: Bearer <token>` 头，否则 401。STDIO 传输不受影响（子进程管道本身即信任边界，不做 token 校验）。
- `KBASE_API_KEY`：KBase API 若已开启鉴权（`create_app(auth="on")`，生产默认），MCP 反调 KBase 的每个请求都需要带凭据。在 KBase 设置页的「API Key」卡片创建一把 key（管理员操作），把完整 key（形如 `kbase_ak_...`，只在创建时展示一次）设成这个环境变量，MCP Server 会自动在所有反调请求上加 `Authorization: Bearer <key>`。未设置且 API 确实要求鉴权时，工具调用会收到清晰的中文错误提示，指引来设置这个变量。

### 注册到 Claude Code

```powershell
claude mcp add kbase -- python -m kbase_mcp
```

### 注册到 Claude Desktop

在 Claude Desktop 的配置文件（`claude_desktop_config.json`）里加入：

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

## 安全与部署

KBase 默认以 `auth="on"` 启动（`create_app` 的默认值），全部 `/api` 路由（除登录本身外）都要求有效的会话 Cookie 或 API Key；下面是私有化部署前需要过一遍的安全清单。

### 首启管理员（bootstrap）

首次启动、`users` 表为空时会自动创建一个 `admin` 账号：

- 设置了环境变量 `KBASE_ADMIN_PASSWORD`：用该值作为初始密码（部署脚本可预设，便于自动化）。
- 未设置：随机生成一个 16 位密码，以 `WARNING` 级别打印到启动日志（`kbase.auth.bootstrap` logger）——**请在首次启动后立即查日志、登录、并考虑改密**。

密码本身从不落库明文，只存 bcrypt 哈希。`users` 表非空之后这套引导逻辑幂等跳过（不会覆盖已有账号）。

### 三级角色与能力矩阵

| 能力 | viewer | editor | admin |
|---|:---:|:---:|:---:|
| 查看知识库/文档列表、检索、问答（含会话） | ✅ | ✅ | ✅ |
| 建库、上传/删除文档、重试解析/OCR、生成任务 | ❌ | ✅ | ✅ |
| Provider 管理、用户管理、API Key 管理、审计日志查询 | ❌ | ❌ | ✅ |

角色是严格序（`viewer < editor < admin`），路由级用 `require_role(min_role)` 声明最低角色；未知/非法角色一律按低于 viewer 处理（拒绝而非放行）。用户由 admin 在设置页的「用户管理」创建，或调用 `POST /api/users {username, password, role}`；系统不允许把最后一个启用中的 admin 禁用或降级，避免自锁。

### API Key 与 MCP 鉴权

管理员可在设置页「API Key」卡片创建一把 key（`POST /api/settings/api-keys {name, role}`），完整 key（形如 `kbase_ak_...`）**只在创建的那一刻返回一次**，之后只能看到 `prefix` 用于辨识；数据库只存 sha256 哈希，吊销（`DELETE /api/settings/api-keys/{id}`）后 Bearer 通道立即拒绝该 key。

`kbase_mcp/` 通过 HTTP 反调这套鉴权后的 API：把创建好的完整 key 设成环境变量 `KBASE_API_KEY`，MCP Server 会自动在每次反调请求上加 `Authorization: Bearer <key>`（详见上方「MCP Server」一节）。角色按 MCP 客户端的实际用途选择——纯问答/检索场景给 viewer 即可，需要建库/上传的自动化流程给 editor。

### 许可证（轻量校验）

`GET /api/license` 返回 `{"status": "trial"|"valid"|"expired"|"invalid", ...}`；未放置证书文件时状态为 `trial`（v1 不锁功能，仅作展示，不拦截任何业务请求）。

签发证书：

```powershell
.venv\Scripts\python scripts\gen_license.py --org "客户名称" --expires 2027-07-06 `
  --private-key D:\Claude Code\kbase-license-private.pem --out license.json
```

- 私钥（`--private-key` 指向的 `.pem`）**必须放在仓库之外**（约定路径 `D:\Claude Code\kbase-license-private.pem`），绝不能提交到 git；首次运行会自动生成密钥对并把公钥打印到终端，需要手工同步进 `kbase/license.py` 的 `_PUBLIC_KEY_B64` 常量。
- 生成的 `license.json` 默认放仓库根目录（已 gitignore）；也可用环境变量 `KBASE_LICENSE_FILE` 指向任意路径（如客户机器上的固定位置）。

### KBASE_SECRET_KEY（会话签名密钥）

会话 Cookie 是一个 HS256 JWT，签名密钥解析顺序：环境变量 `KBASE_SECRET_KEY` 优先；未设置则首次调用时生成一个随机密钥并持久化到 `app_settings` 表。**生产环境应显式设置 `KBASE_SECRET_KEY`**——否则每次重新部署（新容器/新机器，`app_settings` 未随 DB 一起迁移时）都会生成新密钥，导致所有已登录会话的 Cookie 一夜之间全部失效，用户被迫重新登录。

### TLS（生产强制）

**生产部署必须在 KBase 前面挂 TLS（反向代理终结 HTTPS，如 Nginx/Caddy/云负载均衡）**。登录请求把密码明文放在请求体、会话 Cookie 默认只设了 `httponly`/`samesite=lax`（未设 `Secure`），裸 HTTP 部署下密码与会话凭证都会在网络上明文传输，可被中间人窃取。上生产前请：

- 反向代理终结 TLS，KBase 自身继续跑 HTTP（内网/容器间通信）；
- 在反向代理层给 `kbase_session` Cookie 补加 `Secure` 属性（或在代理转发时改写响应头），确保浏览器只在 HTTPS 连接上发送该 Cookie。

### OCR 服务（MonkeyOCR）

扫描件/图片文档摄取时，`config/kbase.yaml` 的 `ocr` 块决定是否调用 MonkeyOCR 做版面识别转 Markdown：

```yaml
ocr:
  enabled: true
  backend: monkey-http
  endpoint: "http://localhost:7861"
```

- `enabled: false`（或服务不可达）时，扫描件/图片摄取优雅降级为 `pending_ocr` / `failed`，不阻塞同批次其余文档，可在 OCR 服务就绪后用「批量重试 OCR」或 `POST /api/documents/{id}/retry` 补跑。
- 仓库当前提交的 `endpoint` 指向开发期一台 GPU 服务器的 SSH 隧道转发地址（本机 7861 转发到远端实际跑 MonkeyOCR 的 GPU 机器），方便开箱即用地演示 OCR 流程；**生产部署必须把它换成生产环境自己的 MonkeyOCR 服务地址**，不能依赖这条开发期隧道。

### 部署（lite / standard）

仓库根目录提供 `Dockerfile` + 两套 `docker-compose.*.yml`。两种 profile 共享同一个应用镜像，区别只是 `config/kbase.*.yaml` 里 embedder/vectorstore/rerank/db 四个插槽指向进程内实现还是外部服务。

#### lite 一键部署

单容器：SQLite + Chroma 嵌入式 + 进程内 bge-m3/reranker（`config/kbase.yaml`），适合演示/小规模验证（单机、无需额外服务）。

```bash
# 项目根目录 .env（已 gitignore）：
#   KBASE_SECRET_KEY=...
#   KBASE_ADMIN_PASSWORD=...       # 可选
#   DASHSCOPE_API_KEY=...
docker compose -f docker-compose.lite.yml up -d --build
```

首次启动注意事项：

- **首次启动会从 HuggingFace 下载 bge-m3/bge-reranker-v2-m3 模型权重**（体积不小），已用命名卷 `hf-cache` 持久化到 `/root/.cache/huggingface`，重建容器不会重新下载；国内网络下载缓慢，可在容器 `environment` 里加 `HF_ENDPOINT=https://hf-mirror.com`。
- **`KBASE_SECRET_KEY` 必须显式设置**（`docker-compose.lite.yml` 用 `${KBASE_SECRET_KEY:?...}` 语法强制要求，未设置直接拒绝启动）——不设置的话每次重建容器都会生成新的会话签名密钥，导致所有已登录用户的 Cookie 一夜之间全部失效。
- `KBASE_ADMIN_PASSWORD`（可选）：首启 admin 的初始密码；不设置则随机生成 16 位密码并打印到容器日志（`docker compose logs app`），只打印这一次，请在首次启动后立即查日志登录并改密。
- 端口：应用监听容器内 `8100`，compose 已映射到宿主机 `8100:8100`（`http://localhost:8100`）。

#### standard 一键部署

`app` + `postgres:16-alpine` + `qdrant/qdrant` + 两个 `text-embeddings-inference`（TEI，embed 一个 + rerank 一个）共 5 个服务，面向生产/较高并发部署（`config/kbase.standard.yaml`）。

```bash
# 项目根目录 .env：
#   KBASE_SECRET_KEY=...
#   KBASE_ADMIN_PASSWORD=...      # 可选
#   DASHSCOPE_API_KEY=...
#   POSTGRES_PASSWORD=...         # standard 独有，PG 元数据库密码
docker compose -f docker-compose.standard.yml up -d --build
```

5 个服务各自的角色：

| 服务 | 作用 |
|---|---|
| `app` | FastAPI 主应用，构建自本仓库 `Dockerfile` |
| `postgres` | 元数据库（kb/document/chunk/用户/审计等关系数据），替代 lite 的 SQLite |
| `qdrant` | 向量库，替代 lite 的 Chroma 嵌入式实例 |
| `tei-embed` | 独立的 embedding 推理服务（`BAAI/bge-m3`），替代 lite 的进程内推理 |
| `tei-rerank` | 独立的重排推理服务（`BAAI/bge-reranker-v2-m3`） |

`config/kbase.standard.yaml` 采用 **bind-mount 约定**：compose 里 `- ./config/kbase.standard.yaml:/app/config/kbase.yaml:ro`，把它只读挂载覆盖镜像内默认的 `config/kbase.yaml`（lite 配置），这样 `create_app()` 的默认参数 `"config/kbase.yaml"` 不用改也能加载 standard 配置——修改这份 YAML（如调 `retrieval.rerank.max_concurrency`、`server.threadpool_size`）后 `docker compose restart app` 即可生效，不需要重新 build。

**GPU vs CPU TEI（计算能力镜像 tag 的坑）**：`docker-compose.standard.yml` 里 `tei-embed`/`tei-rerank` 默认用 `ghcr.io/huggingface/text-embeddings-inference:cpu-latest`（任何机器都能跑通的默认值，正确性不受影响，只是吞吐低得多）。生产 GPU 部署需要手工把镜像 tag 换成对应 **compute capability** 的 GPU 变体、并取消注释 `deploy.resources.reservations.devices`（`driver: nvidia`）——TEI 按 compute capability 发布 tag，不是裸版本号：实测 GCP L4（Ada Lovelace，compute capability 8.9）要用 `89-latest`，其他架构类推（如 Ampere-80 用 `80-latest`）；查错架构会直接启动失败或静默退化到极低吞吐。GPU 与 CPU 镜像输出结果一致，可以先用 CPU 镜像验证功能、确认 GPU 卡型号后再切换。

**国内镜像源要点**：

- `postgres:16-alpine` / `qdrant/qdrant` / TEI 镜像拉取缓慢时，三选一（不要同时改镜像名又配镜像源）：本机 Docker daemon 配置 `registry-mirrors`（如 `docker.m.daocloud.io`）；或把 compose 里的镜像名换成对应的国内加速仓库地址。
- TEI 启动时从 HuggingFace 下载模型权重，国内网络建议给 `tei-embed`/`tei-rerank` 的 `environment` 加 `HF_ENDPOINT=https://hf-mirror.com`（compose 文件里已给出对应行，默认注释，按需打开）。

### 环境变量清单

| 变量 | 必需/可选 | 说明 |
|---|---|---|
| `KBASE_SECRET_KEY` | **必需** | 会话 JWT 签名密钥；未设置会每次重建容器生成新密钥，所有会话失效 |
| `KBASE_ADMIN_PASSWORD` | 可选 | 首启 admin 初始密码；不设置则随机生成并只打印一次到日志 |
| `DASHSCOPE_API_KEY` | **必需** | LLM 网关（百炼/DashScope）密钥，`config/kbase*.yaml` 里 `api_key_env` 指向它 |
| `POSTGRES_PASSWORD` | standard 必需 | PG 元数据库密码，写进 `.env` 供 `postgres` 与 `db.url` 双侧一致 |
| `KBASE_API_KEY`（MCP） | 可选 | `kbase_mcp/` 反调 KBase API 时用的 Bearer key，在设置页「API Key」卡片创建；auth="off" 部署或不用 MCP 时不需要 |
| `KBASE_WAIT_FOR` | 内部/可选 | standard compose 自动设置为 `postgres:5432,qdrant:6333,tei-embed:80,tei-rerank:80`，entrypoint.sh 据此等依赖端口就绪再启动 uvicorn；lite 不设，手工部署一般无需关心 |

### 备份与恢复

**lite**：数据全部在 `./data`（宿主机挂载目录：`kbase.sqlite` + `chroma/` + `files/` 原始文件），停机复制即可保证一致性快照。

```bash
docker compose -f docker-compose.lite.yml stop app
tar -czf kbase-lite-backup-$(date +%F).tar.gz data/
docker compose -f docker-compose.lite.yml start app
```

恢复：停 app，用备份包解压覆盖 `./data`，再启动 app。

**standard**：元数据在 PG，向量在 Qdrant，原始文件在 bind-mount 的 `./data/files`，三者需要分别备份。

```bash
# PG：pg_dump 逻辑备份（建议 nightly cron）
docker compose -f docker-compose.standard.yml exec -T postgres \
  pg_dump -U kbase kbase | gzip > pg-backup-$(date +%F).sql.gz

# Qdrant：官方 snapshot API，对运行中的实例做一致性快照，不需要停机
curl -X POST http://localhost:6333/collections/<collection>/snapshots

# 原始文件卷：直接打包（可在线做，允许有极小的时间窗口不一致，摄取中的文档下次重试即可）
tar -czf kbase-files-backup-$(date +%F).tar.gz data/files
```

恢复各一行：PG 用 `gunzip -c pg-backup-*.sql.gz | docker compose exec -T postgres psql -U kbase kbase` 灌回；Qdrant 用其 snapshot 恢复 API（`PUT /collections/<collection>/snapshots/recover`，指向快照文件）；文件卷解压覆盖 `data/files` 即可。

cron 示例（每天凌晨 3 点跑 PG 备份）：

```
0 3 * * * cd /path/to/kbase-standard && docker compose -f docker-compose.standard.yml exec -T postgres pg_dump -U kbase kbase | gzip > /backups/pg-$(date +\%F).sql.gz
```

### lite → standard 手工迁移

当前**没有自动化迁移工具**，如实说明：lite 的 SQLite/Chroma 数据模型与 standard 的 PG/Qdrant 不是同一套 schema/索引结构，字节级搬迁不现实。迁移思路是**原件重摄取**——lite 环境保留了每个文档的原始文件（`data/files/`），把这些原件重新上传到新建的 standard 栈，让 standard 侧重新走一遍分块/向量化/索引管线：

1. 从 lite 的 `data/files/` 取出所有原始文档（文件名与 `GET /api/kb/{id}/documents` 返回的 `filename` 对应）。
2. 在 standard 栈上建同名知识库（`POST /api/kb`），把原件通过 `POST /api/kb/{id}/documents` 逐个重新上传。
3. 等待摄取完成（`status: ready`），核对文档数量与 lite 侧一致。
4. 会话历史/审计日志等运行时数据不在迁移范围内（如需保留，另行导出 lite 的 SQLite 相应表）。

### 性能与容量规划

诚实记录基于 M4-2 H6/H6.5/H7 三轮实测（GCP `g2-standard-4`，NVIDIA L4 单卡，与常驻 MonkeyOCR 共存，仅 4 vCPU）：

- **全精排舒适区大致在 10 并发以内**：10 并发以下 `POST /api/kb/{id}/search`（TEI-embed 向量化 + Qdrant 稠密检索 + PG 关键词检索 + RRF 融合 + TEI-rerank 重排）P95 亚秒级（约 860ms，接近但尚未完全达到 spec 定的 500ms 线，取决于具体并发量）。
- **100 并发靠自适应降级支撑**：`retrieval.rerank.max_concurrency`（默认 8）限制同时在途的重排调用数，超出的查询不排队等 GPU，直接降级为融合排序（跳过重排但仍走真实的稠密+关键词双路检索，带引用，不是空结果）。降级状态通过 `/healthz` 的 `rerank_stats`（`rerank_total`/`rerank_shed_load_total`/`rerank_error_total`）以及检索 debug trace 的 `rerank_status` 字段可观测，不是静默丢质量。
- **100 并发 P95 实测约 4.2～5.2s**（H6 基线 9.25s → H6.5 重排信号量优化后 4.2s → H7 尝试调大线程池后反而回退到约 5.1～5.2s，详见下一条），是 spec 500ms 验收线的约 8～10 倍，**未达标**；100 并发下 shed-rate 约 62～64%（多数查询走的是降级后的融合排序，非全精排）。
- **要 100 并发下全精排 P95 < 500ms，需要独立的重排 GPU 或更强算力卡**——显存不是瓶颈：实测 24GB 显存的 L4 卡在压测全程只用到约 16GB，且这 16GB 里包含与 MonkeyOCR 共存占用的部分，即使额外释放 13.5GB 显存也无助于改善（瓶颈是 GPU 的计算吞吐与调度排队，不是显存容量）；H7 还实测验证了"调大 AnyIO 线程池容量"这条路线在当前 4-vCPU 机器上无效（甚至略有回退，根因是物理核心数而非线程槽位数）。真正有希望的方向是横向扩展重排算力（独立 GPU 实例、更高算力卡）或减少每次查询的 rerank candidates 数量。

详细压测方法论、逐级数据表、shed-rate 明细与本节结论的完整推导过程见 [`loadtest/report-standard.md`](loadtest/report-standard.md)。

## 架构

内核只依赖抽象接口，具体实现（Embedder/VectorStore/LLMProvider/Chunker）在插件层注册、YAML 配置选择；完整设计（分块策略、混合检索、性能设计、部署 profile、Roadmap）见 [`docs/superpowers/specs/2026-07-04-kbase-knowledge-base-design.md`](docs/superpowers/specs/2026-07-04-kbase-knowledge-base-design.md)，M1 阶段的实施拆解见 [`docs/superpowers/plans/`](docs/superpowers/plans/) 下对应计划文档。

## 已知限制（M1）

- **OCR 依赖外部 MonkeyOCR 服务**：`config/kbase.yaml` 的 `ocr.enabled=true` 时扫描件/图片会调用 MonkeyOCR 转 Markdown（见「安全与部署」一节），未配置或服务不可达时优雅降级为 `pending_ocr`/`failed`，不阻塞其余文档摄取。
- **qwen2.5 开源系列（32B/72B）暂不可用于规模对比**：当前 API Key 未在百炼控制台开通该系列（403），需先开通或改用其他服务商端点才能补齐"36B vs 72B"级别的模型对比。
- **单实例 lite 部署形态**：SQLite + Chroma 嵌入式 + 进程内 bge-m3，适合演示与小规模验证；面向 100+ 并发的生产部署（PostgreSQL + Qdrant + TEI + vLLM）见设计文档第 4 节 standard profile。
