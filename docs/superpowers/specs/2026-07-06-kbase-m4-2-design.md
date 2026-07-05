# KBase M4-2 设计文档 — 生产部署形态（Docker/standard profile/并发实测）

- 日期：2026-07-06
- 状态：授权自主执行（M4 总授权）
- 前置：M4-1 已合并（256 backend / 76 frontend，认证全栈上线）

## 1. 目标

兑现原始 spec §4 的 standard profile 与"Docker Compose 一键部署"卖点，并用真实压测拿到 100 并发指标（原始 spec §11：检索 P95 < 500ms 不含 LLM 生成，系统无错误）——这也是给兵团报 GPU/硬件预算的技术依据。

## 2. standard profile 组件适配（全部走既有插槽，内核零改动）

| 插槽 | lite（现状） | standard 新增实现 |
|---|---|---|
| Embedder | 进程内 bge-m3 | **TEIEmbedder**（HTTP → TEI 服务 /embed，动态 batching 由 TEI 负责） |
| Reranker | 进程内 CrossEncoder | **TEIReranker**（HTTP → TEI /rerank） |
| VectorStore | Chroma 嵌入式 | **QdrantStore**（qdrant-client；单元测试用 `QdrantClient(":memory:")` 本地模式，无需容器） |
| 元数据 | SQLite | **PostgreSQL**（SQLAlchemy URL 即换；`db.url` 进配置） |
| KeywordIndex | SQLite FTS5 | **PGKeywordIndex**：沿用 jieba 预分词+空格连接的既有策略，PG 侧 `to_tsvector('simple', ...)` + GIN 索引 + `plainto_tsquery('simple', ...)` ts_rank 排序——**不装 zhparser 等中文扩展**，分词永远在应用层，两种后端语义一致 |

配置：`db: {url: sqlite:///... | postgresql+psycopg://...}`；migrations 方言感知（FTS5 DDL 仅 SQLite；PG 走 tsvector 列+GIN；ALTER 缺列守卫通用）。KeywordIndex 按 db 方言自动选实现。

## 3. Docker 交付

- **Dockerfile**（单镜像）：python3.11-slim + `pip install .[mcp]` + 构建产物 web/ + entrypoint（等待依赖→启动 uvicorn；bootstrap admin 走 env）。`.dockerignore` 排除 data//web-app/node_modules/.venv/材料。
- **docker-compose.lite.yml**：单服务 + 数据卷（含 HF 模型缓存卷,首启下载 bge/reranker）。
- **docker-compose.standard.yml**：app + postgres:16 + qdrant + tei-embed（bge-m3）+ tei-rerank（bge-reranker-v2-m3）+ 卷与健康检查依赖顺序。GPU 部署时 TEI 用 `--gpus`（compose deploy.resources），CPU 环境自动可跑（TEI CPU 镜像变体注释给出）。
- 国内网络注释：基础镜像与 HF 模型的镜像源替换点。
- MCP 不入 compose（客户按需另起）。

## 4. 部署与压测环境

复用 GCP GPU VM（L4，与 MonkeyOCR 共存——TEI fp16 双模型约 4GB VRAM,现余 7GB,可容纳；RAM 16GB 紧张,压测时监控）。VM 装 docker → 起 standard 栈 → 数据迁移脚本把彩排库灌入（reindex 思路：从本地导出文档原件重摄取,或直接跑摄取）→ 压测。

**压测口径**：目标接口 `POST /api/kb/{id}/search`（含向量化+双路检索+重排,不含 LLM 生成——与 spec §11 口径一致）。梯度 10/50/100 并发,每档 ≥60s,统计 P50/P95/P99/错误率。工具：locust（headless）。另做 20 并发 SSE 问答冒烟（用假 provider 或限次真实调用）验证流式通道无错。鉴权：压测客户端用 API Key。
**验收线**：100 并发下检索 P95 < 500ms 且错误率 0。达不到时的调优抓手（按序）：uvicorn workers、TEI 并发参数、Qdrant 检索参数、DB 连接池——调优后复测,如实报告最终数字。

## 5. 备份策略（文档交付）

README「运维」小节：PG pg_dump 定时、Qdrant snapshot API、files 卷打包、SQLite（lite）单文件拷贝；恢复步骤各一行。不做内置备份调度（部署层 cron）。

## 6. 非目标

K8s/Helm；多节点高可用；蓝绿发布；镜像仓库发布流水线；lite→standard 自动迁移工具（提供手工步骤文档）。

## 7. 风险与预案

VM 磁盘仅余 ~19GB：TEI GPU 镜像较大,不够时先清理 MonkeyOCR pip 缓存/apt 缓存,再不够改 TEI CPU 镜像压测（如实标注口径差异）或申请扩盘（找用户）。RAM 不足时 MonkeyOCR 服务临时停止腾内存（压测期间不影响,压完恢复）。
