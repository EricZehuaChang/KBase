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
.venv\Scripts\uvicorn --factory kbase.api.main:create_app --port 8000
```

浏览器打开 http://localhost:8000 即可使用知识库管理与问答页面。`/healthz` 可查看各插件加载状态。

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

## 架构

内核只依赖抽象接口，具体实现（Embedder/VectorStore/LLMProvider/Chunker）在插件层注册、YAML 配置选择；完整设计（分块策略、混合检索、性能设计、部署 profile、Roadmap）见 [`docs/superpowers/specs/2026-07-04-kbase-knowledge-base-design.md`](docs/superpowers/specs/2026-07-04-kbase-knowledge-base-design.md)，M1 阶段的实施拆解见 [`docs/superpowers/plans/`](docs/superpowers/plans/) 下对应计划文档。

## 已知限制（M1）

- **扫描件 PDF 无 OCR**：摄取管道目前只处理有文本层的文档，纯图片扫描件会被标记为 `failed`（OCR 插槽计划在后续版本接入，详见设计文档 5.1 节）。
- **qwen2.5 开源系列（32B/72B）暂不可用于规模对比**：当前 API Key 未在百炼控制台开通该系列（403），需先开通或改用其他服务商端点才能补齐"36B vs 72B"级别的模型对比。
- **单实例 lite 部署形态**：SQLite + Chroma 嵌入式 + 进程内 bge-m3，适合演示与小规模验证；面向 100+ 并发的生产部署（PostgreSQL + Qdrant + TEI + vLLM）见设计文档第 4 节 standard profile。
