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

### Docker 部署（lite / standard）

仓库根目录提供 `Dockerfile` + 两套 `docker-compose.*.yml`：

- `docker-compose.lite.yml`：单容器，SQLite + Chroma 嵌入式 + 进程内 bge-m3/reranker（对应 `config/kbase.yaml`），适合演示/小规模验证。
- `docker-compose.standard.yml`：`app` + `postgres:16-alpine` + `qdrant/qdrant` + 两个 `text-embeddings-inference`（embed/rerank）实例（对应 `config/kbase.standard.yaml`），面向 100+ 并发生产部署。

两者都需要先设置 `KBASE_SECRET_KEY`（会话签名密钥，见上文）、`KBASE_ADMIN_PASSWORD`（可选，首启管理员密码）、`DASHSCOPE_API_KEY`（LLM 网关密钥），standard 额外需要 `POSTGRES_PASSWORD`；这些变量可写进项目根目录的 `.env`（已 gitignore）供 `docker compose` 自动读取。

完整的部署运维手册（备份/恢复、lite→standard 迁移步骤、压测结论）见后续「运维文档」章节；本节仅是最小指路，真实 VM 构建与实测见 M4-2 的 H5/H6 任务记录。

## 架构

内核只依赖抽象接口，具体实现（Embedder/VectorStore/LLMProvider/Chunker）在插件层注册、YAML 配置选择；完整设计（分块策略、混合检索、性能设计、部署 profile、Roadmap）见 [`docs/superpowers/specs/2026-07-04-kbase-knowledge-base-design.md`](docs/superpowers/specs/2026-07-04-kbase-knowledge-base-design.md)，M1 阶段的实施拆解见 [`docs/superpowers/plans/`](docs/superpowers/plans/) 下对应计划文档。

## 已知限制（M1）

- **OCR 依赖外部 MonkeyOCR 服务**：`config/kbase.yaml` 的 `ocr.enabled=true` 时扫描件/图片会调用 MonkeyOCR 转 Markdown（见「安全与部署」一节），未配置或服务不可达时优雅降级为 `pending_ocr`/`failed`，不阻塞其余文档摄取。
- **qwen2.5 开源系列（32B/72B）暂不可用于规模对比**：当前 API Key 未在百炼控制台开通该系列（403），需先开通或改用其他服务商端点才能补齐"36B vs 72B"级别的模型对比。
- **单实例 lite 部署形态**：SQLite + Chroma 嵌入式 + 进程内 bge-m3，适合演示与小规模验证；面向 100+ 并发的生产部署（PostgreSQL + Qdrant + TEI + vLLM）见设计文档第 4 节 standard profile。
