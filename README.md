# KBase 私有化知识库系统

KBase 是面向企业的**单租户**私有化知识库应用：文档摄取、混合检索、带引用的问答、内容治理与外部系统接入。后端为 Python / FastAPI，前端是 Vue 3 双入口应用（使用端 + 管理端），支持 Docker Compose 部署。

两条部署路线共用同一个应用镜像，只靠挂载的配置切换插槽：

- **lite**：单容器，SQLite + 嵌入式 Chroma + 进程内 bge-m3 / bge-reranker-v2-m3。适合演示与小规模私有化交付，不需要 GPU。
- **standard**：app + PostgreSQL 16 + Qdrant + 两个 TEI（embed / rerank）共 5 个服务，面向生产与较高并发。

> 本文核对日期：2026-09-14；工程基线为本仓库 `main` 的 `25117bc`。**下文所有命令默认在 KBase 仓库根目录执行。**
>
> 先了解三件事：①API Key 的库白名单没有覆盖 `/v1` 兼容接口；②数值范围 / approx 形式的元数据 filters 会被 HTTP 请求校验拒绝；③standard 的数据库密码不会自动注入，只填 `.env` 无法完成配置。详见「已知限制」。

## 能力与边界

| 模块 | 当前实现 |
|---|---|
| 文档摄取 | PDF、Office、Markdown、网页、图片；自动 / OCR / VLM 三种解析路径，VLM 识别结果需人工确认后才入库 |
| 结构保留 | 标题层级父子分块、表格行组、重复表头、跨页断表合并；Markdown front matter 元数据随块保存 |
| 检索问答 | 向量 + BM25 关键词双路召回、RRF 融合、可选重排与过载降级、多轮查询改写、多库联查；全局 / 库级 / 请求级三级策略 |
| 引用与运营 | 文档与章节引用、PDF 页码定位、命中图文章节附原图、分块编辑与启停、反馈、无答案清单、评测集（hit@k / MRR） |
| 生成任务 | 方案大纲与逐节生成、文档汇编、后台任务进度、Markdown / DOCX 导出 |
| 身份与权限 | 本地账号、viewer / editor / admin / superadmin、自定义权限集合、库级 ACL、API Key（含库白名单）、OIDC 实现、审计日志 |
| 集成 | REST / SSE、OpenAI 兼容接口、三个 MCP 工具、免登录分享与 widget、飞书机器人、飞书定时增量同步连接器 |
| 页面 | 使用端 `/`；管理端 `/admin/`；中文 / 英文 / 马来语界面、亮暗主题、翻译覆盖管理 |
| 运维 | lite / standard 配置、启动期幂等数据库迁移、备份恢复脚本、健康与指标端点、GitHub Actions 门禁与源码发版包 |

**私有化部署不等于默认全离线。** lite 的向量化与重排在进程内完成，但 LLM、GLM-OCR 与 VLM 可以且默认会调用云服务；完全离线需要提前准备依赖与模型缓存，并把所有在用的模型通道切换为可用的本地服务。引用与拒答机制也不等于绝对不会生成错误答案。

## 架构与目录

```text
浏览器 / 外部 API / MCP
           ↓
FastAPI：身份解析、权限、路由、任务与静态资源
           ↓
摄取 → 解析 / 人工确认 → 结构分块 → 向量索引 + 关键词索引
                                          ↓
提问 → 可选改写 → 混合召回 → 重排 → 上下文组装 → LLM + 引用
```

```text
kbase/
  api/              服务装配、请求模型、领域路由、双入口静态托管
  auth/             登录、角色权限、OIDC、引导账号
  ingest/           文档解析、front matter、摄取管线
  rag/              检索、改写、回答生成
  plugins/          向量模型、向量库、LLM、OCR、分块、重排、增强适配器
  index/            SQLite / PostgreSQL 关键词索引
  jobs/             方案生成、汇编与 DOCX 导出
  params.py         表格参数解析、单位归一、块级范围匹配
  connectors.py     飞书增量同步与调度
  migrations.py     幂等数据库迁移
kbase_mcp/          独立 MCP 进程，通过 HTTP 调用主应用
web-app/            Vue 3 + TypeScript + Vite + Tailwind 4 + reka-ui 源码（双 HTML 入口）
web/                已提交的前端构建产物（index.html / admin.html），镜像直接使用
config/             lite / standard YAML
scripts/            配置检查、备份恢复、许可证签发、开发工厂
tests/              Python 测试
eval/               问答评测脚本与题库
loadtest/           历史压测脚本与报告
.github/workflows/  CI、镜像冒烟、nightly、release
```

代码入口：[API 工厂](kbase/api/main.py)、[服务装配](kbase/api/services.py)、[请求模型](kbase/api/schemas.py)、[库级权限](kbase/kb_acl.py)、[角色与权限词表](kbase/auth/roles.py)。完整设计（分块策略、混合检索、性能设计、部署 profile、路线图）见 [设计文档](docs/superpowers/specs/2026-07-04-kbase-knowledge-base-design.md)，分阶段实施拆解见 [`docs/superpowers/plans/`](docs/superpowers/plans/)。

## 快速开始：Docker lite

前置：Docker Engine / Desktop 与 Compose 插件，以及模型下载和所选模型 API 的网络访问。首次启动会下载本地 embedding 与 reranker 权重（bge-m3 + bge-reranker-v2-m3，体积不小），耗时取决于缓存、网络与机器。

1. 在**仓库根目录**创建 `.env`，替换所有占位值：

```dotenv
KBASE_SECRET_KEY=<随机生成的长密钥>
KBASE_ADMIN_PASSWORD=<首启管理员密码>
ZHIPU_API_KEY=<使用默认LLM和GLM-OCR时需要的密钥>
# 仅在启用相应通道时填写：
# DASHSCOPE_API_KEY=...
# DEEPSEEK_API_KEY=...
# OPENAI_API_KEY=...
# MOONSHOT_API_KEY=...
# IRUIDONG_API_KEY=...
```

`KBASE_SECRET_KEY` 可用 `python -c "import secrets; print(secrets.token_urlsafe(48))"` 生成。`.env` 已在 `.gitignore` 中，不要提交真实密钥。lite 默认的 LLM 是 `glm-5-turbo`、OCR 是 `glm-ocr`，两者都读 `ZHIPU_API_KEY`；这是当前配置里写死的默认值，不代表对应厂商服务已在本轮验证可用。

2. 启动并检查：

```bash
docker compose -f docker-compose.lite.yml up -d --build
docker compose -f docker-compose.lite.yml ps
docker compose -f docker-compose.lite.yml logs --tail=100 app
curl -fsS http://localhost:8100/healthz
```

打开 `http://localhost:8100/`（使用端）与 `http://localhost:8100/admin/`（管理端）。`users` 表为空时会创建用户名 `admin`、角色 `superadmin` 的账号：设置了 `KBASE_ADMIN_PASSWORD` 就用它，否则随机生成并**只打印一次**到启动日志。`users` 表非空后引导逻辑幂等跳过，再改 `KBASE_ADMIN_PASSWORD` 不会重置已有密码。

3. 登录管理端确认所选 Provider 连通；创建知识库，上传小份样本文档，等状态变为 `ready`；用一个**能从原文回答**的问题检查答案、引用与原件预览。

`/healthz` 成功只说明应用及其自报组件就绪，不能替代真实问答、OCR 或外部模型验收。

数据持久化在 `./data`，模型缓存使用 `hf-cache` 命名卷。缓存完整后可设 `HF_HUB_OFFLINE=1` 跳过启动期的 Hugging Face 联网校验（HF 镜像站故障会卡启动）；它不会把云 LLM / OCR 变成离线服务。lite 的 YAML 在构建时复制进镜像，直接改宿主机 YAML 后需重建镜像，或自行增加配置挂载。

## 本地开发

后端以 **Python 3.11** 为 CI / 镜像基线（包声明 `requires-python = ">=3.11"`）。文本层 PDF 的 OpenDataLoader 主解析器需要 Java 11+，CI 与镜像使用 Java 21；没有可运行的 Java 时自动回退到 markitdown（只损失 PDF 结构质量）。

macOS / Linux：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,mcp,local-embed]'
python -m uvicorn --factory kbase.api.main:create_app \
  --env-file .env --host 127.0.0.1 --port 8100
```

Windows PowerShell（已安装 Python 3.11）：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,mcp,local-embed]"
.venv\Scripts\python -m uvicorn --factory kbase.api.main:create_app --env-file .env --host 127.0.0.1 --port 8100
```

`create_app` 本身不读取 `.env`，上面通过 Uvicorn 的 `--env-file` 显式加载。工作目录必须是仓库根，默认配置路径为 `config/kbase.yaml`。

只做本机界面联调时可以用开发工厂：`python -m uvicorn --factory scripts.dev_app:create_dev_app --host 127.0.0.1 --port 8100`。它关闭鉴权、默认使用假向量，**不能用于生产或检索效果验收**；其中配置的云模型调用仍可能真实发生。

前端在另一个终端执行：

```bash
cd web-app
nvm use          # 已装 nvm 时读取 .nvmrc（Node 22）
npm ci
npm run dev
```

未用 nvm 时请自行选择 Node 22。Vite 默认端口 5173，`/api` 与 `/healthz` 已代理到后端 8100。前端改动必须运行 `npm run build`、`npm run check-isolation`，并把重新生成的 `web/` 一并提交。不要直接编辑 `web/`；Node 26 起自带实验性 `localStorage`，会让 vitest 的 jsdom 环境拿不到 localStorage（已记录的不兼容）。

## 配置规则

| 配置 | 生效方式 |
|---|---|
| `config/kbase.yaml` | lite 默认：bge-local / Chroma / glm-5-turbo / GLM-OCR；重排与改写未在 YAML 写出的键由 `kbase/config.py` 补默认值 |
| `config/kbase.standard.yaml` | standard：TEI / Qdrant / PostgreSQL，Compose 只读挂载到容器默认配置路径；LLM 清单与 lite **不同步**（当前为 qwen-plus 等 DashScope 通道，OCR 仍是外部 monkey-http） |
| LLM providers / active | **仅 providers 表为空时从 YAML 种子导入**；此后通过管理页面或 API 修改数据库配置，改 YAML 不再生效 |
| Provider / embedding API Key | 页面保存的 DB 值优先于对应环境变量；对外显示脱敏，这不等于数据库内的密钥已加密 |
| 库级 embedding | 建库时绑定；换绑会触发全库重新嵌入，原模型的向量不能混用 |
| 检索策略 | 全局默认 → 库级覆盖 → 检索请求级试跑覆盖 |
| OCR / VLM | lite 默认智谱云 GLM-OCR；standard 配置为外部 MonkeyOCR；VLM 必须选择支持图像输入的通道 |

配置加载器只做 YAML / Pydantic 解析，**不做任意环境变量插值**；`resolve_db_url` 只替换 `{data_dir}` 占位符。`retrieval.rewrite.mode` 的 `"off"` 必须加引号，否则会被 YAML 解析成布尔值而校验失败。

### 检索策略与多轮改写

检索的关键默认值在 `kbase/config.py` 的 `RetrievalConfig`（`config/kbase.yaml` 未逐项写出，按需覆盖）：

```yaml
retrieval:
  hybrid: true          # false = 只用向量路
  candidates: 20        # 每路召回数与融合候选数
  rrf_k: 60
  rerank:
    enabled: true       # lite 默认进程内 bge-reranker-v2-m3；超并发时降级为融合排序
    max_concurrency: 8  # 超出这个数的查询不排队，直接跳过重排
  rewrite:
    mode: "conditional" # off | conditional | always；必须带引号
    max_wait_s: 5.0
```

`rewrite` 用于会话问答中的追问（如「那司局级呢？」）：`conditional` 只在启发式判断为依赖上下文的追问时改写，`always` 只要有历史就改写，`off` 完全关闭。改写失败（超时 / 报错 / 空输出）一律静默回退为原文，不阻塞主链路；触发情况以 `INFO` 记在 `kbase.rag.rewriter` logger。改写只影响检索，落库与回答仍用用户原文。

环境变量速查：

| 变量 | 用途 / 条件 |
|---|---|
| `KBASE_SECRET_KEY` | Compose 必填；会话 JWT 签名。原生启动缺省时生成随机密钥并存入 `app_settings`，保留 DB 的重启会复用 |
| `KBASE_ADMIN_PASSWORD` | 仅 `users` 表为空的首启引导使用 |
| `ZHIPU_API_KEY` / `DASHSCOPE_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `MOONSHOT_API_KEY` | 各 provider 的 `api_key_env` 指向的密钥；按实际启用的通道配置，没有哪一家对所有部署都必需 |
| `IRUIDONG_API_KEY` | YAML 已内置 4 个 iruidong 中转通道，但 lite Compose 未透传该变量，需要时自行在 `environment` 补映射 |
| `POSTGRES_PASSWORD` | standard 的 PG 容器密码；**不会自动改写应用 YAML 里的 `PASSWORD`** |
| `HF_ENDPOINT` / `HF_HUB_OFFLINE` | 模型下载端点 / 已缓存模型的离线加载 |
| `KBASE_PDF_PARSER` | 设为 `markitdown` 可强制使用 PDF 回退解析器 |
| `KBASE_LICENSE_FILE` | 许可证文件路径；容器使用时还要显式挂载文件并传入变量 |
| `KBASE_WAIT_FOR` | standard Compose 自动设为 `postgres:5432,qdrant:6333,tei-embed:80,tei-rerank:80`，`entrypoint.sh` 据此等待依赖端口后启动 Uvicorn |
| `KBASE_API_URL` / `KBASE_API_KEY` | MCP 调用主应用的地址与凭据 |
| `KBASE_MCP_TOKEN` | MCP HTTP 传输的独立 Bearer token；未配置即不启用这层校验 |
| `KBASE_MCP_FILTERS_DOC` | 为 MCP 工具说明追加业务元数据词表 |

Compose 的 `.env` 主要用于变量插值，**不会**把其中所有变量自动注入容器；要确认某个变量是否进入进程，请看对应服务的 `environment` / `env_file`。仓库当前没有受跟踪的 `.env.example`，请以 Compose 文件与本节表格为准。

## REST、MCP 与分享接入

REST 使用登录 Cookie 或 `Authorization: Bearer <API Key>`。`/docs`、`/redoc` 与 `/openapi.json` **只在 `auth="off"` 的本机联调下提供**（生产 `auth="on"` 时主动关闭，避免未鉴权暴露全量路由与 schema）。常用端点：

| 方法与路径 | 用途 |
|---|---|
| `GET /api/kb` | 知识库列表（受 ACL 与 API Key 白名单过滤） |
| `POST /api/kb` | 创建知识库 |
| `POST /api/kb/{kb_id}/documents` | 上传文档 |
| `POST /api/kb/{kb_id}/search` | JSON 检索结果，支持 debug trace 与策略试跑 |
| `POST /api/kb/{kb_id}/query` | SSE 问答，主要事件为 citations → token → done |
| `GET /api/kb/{kb_id}/documents`、`GET /api/documents/{doc_id}/original` | 文档清单与原件下载/预览 |
| `POST /api/proposals/outline`、`POST /api/jobs` | 方案大纲与后台生成任务 |
| `GET /api/jobs/{job_id}/artifact?format=md\|docx` | 下载任务产物 |
| `GET /v1/models`、`POST /v1/chat/completions` | OpenAI 兼容接入；`model` 填库 ID 或唯一库名 |

当前可通过 HTTP 使用的元数据 filters 示例（字段间 AND、列表内 OR）：

```json
{
  "query": "查找制造业知识库方案",
  "top_k": 5,
  "filters": {"industry": "制造", "data_entities": ["订单", "库存"]}
}
```

这些字段来自已摄取文档的 front matter，不是系统自动生成的业务标签。`{"功率": {"gte": 450, "lte": 550}}` 与 `{"approx": 500, "tol": 0.1}` 在底层检索代码中已有实现与测试，但当前请求模型 `_validate_filters` 只接受标量或标量列表，走 HTTP 会返回 422；修复前不能把它当作 HTTP / MCP 已交付的能力。

MCP 依赖一个正在运行的 KBase API，再在已安装 `.[mcp]` 的环境启动：

```bash
export KBASE_API_URL=http://127.0.0.1:8100
export KBASE_API_KEY='<已创建的 API Key>'
python -m kbase_mcp                            # STDIO（默认传输）
# 或配置 KBASE_MCP_TOKEN 后使用 Streamable HTTP：
python -m kbase_mcp --http --host 127.0.0.1 --port 3001
```

工具为 `list_knowledge_bases`、`search_knowledge`、`ask_knowledge_base`；工具失败时不抛协议级异常，而是返回带 `error` 字段的对象，客户端应先检查该字段。MCP 服务不再加载一份模型内核。

API Key 创建接口（`POST /api/settings/api-keys`）支持 `scope_kb_ids`，但管理端新建表单只提交名称与角色，暂未提供库选择。**该白名单在原生检索与库列表路径生效，但没有覆盖 `/v1` 兼容接口**：实测一把只授权 A 库的 viewer key 仍能从 `/v1/models` 列出 B 库，并能对 B 库发起 `/v1/chat/completions`。修复前不要把它当作整个服务的隔离边界。

分享链接支持单库 / 多库绑定与撤销，`/share/{token}` 面向持链接者免登录访问。正式定时同步连接器目前只支持飞书；`POST /api/kb/{kb_id}/import-url` 是单页 URL 导入，不等于通用网站爬虫，也没有 Notion / Confluence 连接器。

## 身份、权限与数据边界

内置角色为 viewer、editor、admin、superadmin（严格序，superadmin 在管理体系之外）；自定义角色可组合 `content.manage`（内容管理）、`system.admin`（设置组）、`audit.view`（审计与运营看板）三种权限。首启账号是 superadmin，普通 admin 不能查看或管理超管账号。库 ACL 目前按用户授权：某库没有 grant 行时对所有已登录用户公开，有 grant 行后按授权用户、owner 与内置管理员判断。

生产入口应使用 HTTPS，并限制未授权网络对应用、数据库与指标端点的访问。登录 Cookie 目前设置了 `HttpOnly` 与 `SameSite=Lax`，**没有设置 `Secure`**，需要在 HTTPS 代理层补齐或完善应用配置；`/metrics` 是应用级端点，不带鉴权。SSO 已有 OIDC 实现与测试，但真实客户 IdP 联调仍是待验收事项。

许可证为 Ed25519 签名校验，`GET /api/license` 返回 trial / valid / expired / invalid；**当前只展示状态，过期不会自动停服，也没有 edition / seats 强制执行**。签发脚本：

```bash
python scripts/gen_license.py --org "<客户名称>" --expires 2027-07-06 \
  --private-key /path/outside/repo/kbase-license-private.pem --out license.json
```

私钥必须放在仓库之外，绝不能提交。首次运行会生成密钥对并把公钥打印到终端，需要手工同步进 `kbase/license.py` 的 `_PUBLIC_KEY_B64` 常量。`license.json` 默认落在仓库根（已 gitignore），也可用 `KBASE_LICENSE_FILE` 指向任意路径（容器内需显式挂载）。

## standard 部署准备

standard Compose 含 app、PostgreSQL 16、Qdrant、TEI embedding、TEI rerank 五个服务；TEI 默认使用 CPU 镜像。GPU 选型、镜像版本与资源配置必须按目标机器验证，不能直接套用历史机器的吞吐数据。

执行启动命令前必须：

1. 配置 `KBASE_SECRET_KEY`、`POSTGRES_PASSWORD` 与所选 LLM 的密钥。
2. **为应用准备一份受保护的部署配置**，使 `db.url` 中的密码与 PG 一致。`config/kbase.standard.yaml` 里写的是字面 `PASSWORD` 占位符，而配置加载器与 `entrypoint.sh` 都不做环境变量渲染（该文件顶部“会被环境变量渲染”的注释是不准确的），只填 `.env` 会导致 PG 与应用密码不一致。需要 URL 编码的密码必须正确编码，含真实凭据的配置不得提交。
3. 配置 OCR 地址、LLM 通道及其环境透传。standard Compose 的 app 服务只透传 `KBASE_SECRET_KEY`、`KBASE_ADMIN_PASSWORD`、`DASHSCOPE_API_KEY` 与 `KBASE_WAIT_FOR`，其他通道密钥需要自行加进 `environment`；standard 的默认模型清单与 lite 不同步，不能假设两份 YAML 的业务配置一致。
4. OCR 走 `host.docker.internal:7861`，Linux 宿主依赖 Compose 里已有的 `extra_hosts: host.docker.internal:host-gateway`；Docker Desktop 默认支持。

完成配置后：

```bash
docker compose -f docker-compose.standard.yml up -d --build
docker compose -f docker-compose.standard.yml ps
curl -fsS http://localhost:8100/healthz
```

TEI 按 compute capability 发布镜像 tag，不是裸版本号：GPU 部署要换 tag 并取消注释 `deploy.resources.reservations.devices`，架构选错会直接启动失败或退到极低吞吐。`scripts/deploy_standard_gcp.md` 记录了一次 GCP L4 机器上的实测部署过程，可作为参数与坑位参考，其中机器与镜像版本已过时。

从 lite 迁移到 standard **目前没有自动化迁移工具**。重摄取原件只是重建知识内容，不会自动迁移账号、ACL、会话、审计、分享与连接器状态；这些需要单独的迁移方案与验收。不要把它描述为无损平滑升级。

## 备份、恢复与升级

备份至少覆盖完整 `data_dir`，以及受保护的部署配置、密钥与许可证。普通上传的原件在 `data/uploads/`，解析后的 Markdown 与插图在 `data/files/`；批量导入的 `Document.source_path` 还可能指向 data_dir 之外的原件目录，需另行纳入备份。只备 `data/files/` 会漏掉上传原件。

lite（源码 checkout 且有可用 Python 环境时）：

```bash
python scripts/backup.py backup --config config/kbase.yaml --out backups
# 恢复前先停应用；把文件名换成你实际生成的备份包：
python scripts/backup.py restore --archive backups/kbase-<时间戳>.tar.gz \
  --config config/kbase.yaml
```

脚本对 SQLite 做在线快照，对 Chroma 与文件目录做普通复制，因此**跨存储的严格一致性仍需停止写入或停服**；它也会排除 `data/backups` 自身，避免把历史备份反复打进新备份。恢复会把原数据目录改名保留，确认无误后再手工删除。脚本使用系统临时目录，若需要把临时文件限制在项目内，请先设置 `TMPDIR`。

standard 需要分别备份 PG、Qdrant 与完整文件目录；恢复时先停应用，再依次恢复数据库、向量与文件，随后检查登录、文档数、原件预览与引用。`scripts/backup.py` 只覆盖本地 `data_dir`，不会自动备份外部 PG / Qdrant。Qdrant 可用其 snapshot API 做不停机快照，PG 用 `pg_dump`：

```bash
docker compose -f docker-compose.standard.yml exec -T postgres \
  pg_dump -U kbase kbase | gzip > pg-backup-$(date +%F).sql.gz
curl -X POST http://localhost:6333/collections/<collection>/snapshots
```

升级顺序：保存版本与依赖信息 → 备份并确认可恢复 → 停止写入 → 替换代码 / 镜像 → 启动迁移 → 验收。回滚要同时考虑代码、数据库与向量存储版本；不要把 Chroma 1.x 的持久化数据交给 0.x 运行时（开发测试环境用 0.x，生产跑 1.5.9，见下文测试一节）。

**交付包边界**：当前 Docker 镜像只 COPY `kbase/`、`kbase_mcp/`、`web/`、`config/` 与 `entrypoint.sh`，不含 `scripts/`；release 的 `git archive` 白名单也不含 `scripts`、`README.md`、`docs/`。因此**只拿到运行包的人无法照着本文执行 `python scripts/backup.py`，也拿不到手册**；需要从同版本源码仓库取得这些文件。这是待补的交付缺口。

## 测试与 CI

测试环境与生产环境分离。常规 CI 使用 Python 3.11、Java 21、Node 22。为避开 Chroma 1.x 在测试进程中反复创建 client 的线程膨胀，常规测试单独安装 `chromadb<1`；**生产依赖不加该上界**（线上跑的就是 1.5.9），nightly 另有 1.5.9 金丝雀守护。

```bash
# 独立测试虚拟环境；从仓库根执行
python -m pip install -e '.[dev,mcp]' 'chromadb<1'
python scripts/check_config.py
python -m ruff check kbase kbase_mcp tests scripts eval
python -m pytest
```

`pytest` 默认排除 `external` 与 `pg` 标记。真实 PostgreSQL 就绪后设置 `KBASE_TEST_PG_URL` 再跑 `python -m pytest -m pg`；外部 API / 模型用例用 `python -m pytest -m external -v`，需要真实服务且可能产生调用费用。

前端使用 Node 22，在 `web-app/` 下依次：`npm ci` → `npm test` → `npm run build`（`vue-tsc` 类型检查 + Vite 双入口构建到 `../web`）→ `npm run check-isolation`，最后确认 `web/` 与提交内容一致（CI 会检查漂移，不一致直接红）。

Docker 只做导入冒烟时**必须显式覆盖 ENTRYPOINT**：

```bash
docker build -t kbase:ci .
docker run --rm --entrypoint timeout kbase:ci 120 \
  python -c "import kbase.api.main, kbase_mcp.server, opendataloader_pdf; print('ok')"
docker run --rm --entrypoint timeout kbase:ci 240 \
  python -c "import sentence_transformers; print('ok')"
docker run --rm --entrypoint sh kbase:ci -c 'java -version'
docker run --rm --entrypoint sh kbase:ci -c 'test -f /app/web/index.html && test -f /app/web/admin.html'
```

镜像的 `ENTRYPOINT` 是 `entrypoint.sh`，它固定 `exec uvicorn` 且**忽略附加参数**：省略 `--entrypoint` 会变成启动真应用并触发模型下载，而不是执行你期望的导入检查（历史 CI 上真踩过，单步挂 26 分钟）。

完整门禁见 [`.github/workflows/`](.github/workflows/)：`ci.yml` 壳 + `ci-core.yml` 四个 job（ruff 与配置可加载、pytest、PG 集成、前端单测 / 构建 / 隔离 / 产物漂移）、`docker-ci.yml`（main 的镜像构建与冒烟）、`nightly.yml`（Chroma 1.5.9 金丝雀、依赖审计、每日镜像、全量回归）、`release.yml`（`v*` tag 门禁 + 源码打包挂 Release）。当前**没有**把镜像推送到 GHCR 的流程。

## 已知限制

以下限制按本轮（2026-09-14）代码核对结果列出，与基线 `25117bc` 对应：

| 优先级 | 当前缺口 |
|---|---|
| P0 | `/v1/models` 与 `/v1/chat/completions` 未应用 API Key 的 `scope_kb_ids`（本轮用真实鉴权依赖 + 内存合成数据复现：受限 key 可列出并访问白名单外的库） |
| P1 | 数值范围 / approx filters 的底层实现与请求模型脱节：`SearchBody` / `QueryBody` 对标量与列表返回 200，对 `gte/lte` 与 `approx/tol` 返回 422 |
| P1 | standard 的 DB 密码没有自动注入闭环；lite Compose 缺 `IRUIDONG_API_KEY` 透传；仓库无受跟踪的 `.env.example` |
| P1 | 运行包与 release 包不含备份脚本和文档；旧备份说明漏了 `uploads`；`docs/manual/运维手册.md` 的健康 / Prometheus 示例仍写 8000（当前 lite 与 standard 的 Compose 都是 8100） |
| P2 | 包版本仍是 `0.1.0`（前端 `0.0.0`），CHANGELOG 停留在 v1.0.x；Python 依赖为版本范围且 `qdrant/qdrant`、TEI 使用 `latest` 类标签，尚未形成固定 digest 的物料清单 |

其他必须如实说明的边界：

- 参数范围匹配是**块级 min/max 区间相交**，不是逐行精确筛选。旧文档的 `Chunk.layout` 若没有 params，单纯运行 `python -m kbase.reindex --kb <kb_id>` 只会复制既有 layout，不会补算参数；这类文档需要重新解析 / 分块，或专门做参数回填。
- 本次核对只做了静态代码检查与少量最小探针，未重跑完整 pytest 与前端测试，未启动 standard 栈，未做真实备份恢复演练，也未访问生产环境。历史 CI 的 611 后端 / 171 前端通过数属于当时基线，不代表当前 HEAD。
- 引用溯源不做逐页 bbox 高亮，这是已记录的产品取舍，不算遗漏项。
- 真实 IdP 联调、更多连接器与源权限同步、视频 / ASR 时间戳、Deep Research 与编排属于后续方向，**尚未上线**，不应作为现成能力对外承诺。
- 压测数字（10 并发 P95 亚秒、100 并发 P95 约 4.2～5.2s 且约 6 成查询降级）来自特定机器上的一次实测，其中 100 并发**未达** 500ms 验收线；详见 [`loadtest/report-standard.md`](loadtest/report-standard.md)，不能当作当前环境 SLA。

历史资料可作参考，但其中旧部署参数与旧表述需按本文与当前代码重新核对：[CHANGELOG.md](CHANGELOG.md)、[完整手册](docs/manual/KBase-完整手册.md)、[运维手册](docs/manual/运维手册.md)、[压测报告](loadtest/report-standard.md)、[Agent Profile](AGENT-PROFILE.md)。
