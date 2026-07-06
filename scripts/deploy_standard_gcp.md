# H5：standard profile 部署到 GCP GPU VM（实测记录）

日期：2026-07-06
分支：`feature/m4-2`（commit 4c69422 起）
目标机器：`instance-20260622-124009`（zone `us-central1-b`，g2-standard-4，
NVIDIA L4 / 23034 MiB 显存，Ubuntu 22.04，~16GB RAM，~97GB 磁盘）。
该机器上已常驻运行 MonkeyOCR（uvicorn :7861，占用显存约 16080 MiB），本次
部署与其共存，全程未停止/重启 MonkeyOCR，未改动实例规格。

## 结论摘要

- Docker Engine + compose plugin：全新安装，成功。
- nvidia-container-toolkit：机器上已预装，仅需 `nvidia-ctk runtime configure
  --runtime=docker` + 重启 docker daemon 即可让容器拿到 GPU。
- TEI 资源策略：**GPU 模式跑通**（bge-m3 + bge-reranker-v2-m3 两个 GPU 容器
  与 MonkeyOCR 共存，共占约 2.7GB 显存，23GB 显存总量下富余），未降级到 CPU。
- 发现并修复一个真实构建才会暴露的 bug：`entrypoint.sh` 经 Windows checkout
  （`core.autocrlf=true`）+ `git archive` 后被转成 CRLF，容器内 `#!/bin/sh\r`
  shebang 解析失败，报 `exec /app/entrypoint.sh: no such file or directory`。
  修复：新增 `.gitattributes`（`*.sh text eol=lf`），强制 shell 脚本始终以
  LF 入库/检出，不受检出平台的 autocrlf 设置影响。
- compose 里此前标注的"H5 待验证"风险（qdrant/TEI 镜像的 healthcheck 用
  `bash -c '... /dev/tcp/...'`，担心镜像 shell 是 busybox ash 而非 bash）
  **未发生**：两个镜像都自带 bash，`/dev/tcp` 探测按预期工作，healthcheck
  全部正常转为 healthy，无需改动。
- 5 个 `@pytest.mark.pg` 集成测试（此前从未在真实环境跑过）：**全部通过**。
- 全链路冒烟（登录→建 Key→建库→传中文文档→轮询 ready→问答引用→检索
  debug trace）：**通过**，答案正确引用了上传文档内容，且 debug trace
  证实 dense（Qdrant+TEI-embed）/keyword（PG keyword_pg）/fused/reranked
  （TEI-rerank）四段检索全部生效。

## 步骤记录

### 1. 安装 Docker + GPU 支持

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh                    # Docker Engine 29.6.1 + compose v5.3.0
sudo usermod -aG docker $USER            # 需要新开一个 SSH 会话才生效
sudo systemctl enable docker
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
# 验证：docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

nvidia-container-toolkit 在这台机器上已经预装好（大概率是配 MonkeyOCR 时装的），
只缺 Docker 侧的 runtime 配置这一步。

### 2. 上传构建上下文

本地（Windows）用 `git archive feature/m4-2 -o kbase-standard.tar` 打包，
`gcloud compute scp` 传到 VM，`tar -xf` 解到 `~/kbase-standard/`。

**踩坑**：`git archive` 在 Windows checkout（`core.autocrlf=true`）下会把
LF 转成 CRLF，导致 `entrypoint.sh` 在容器里 exec 失败（见上文"结论摘要"）。
当时用 `sed -i 's/\r$//' entrypoint.sh` 在 VM 上临时修复并重新 `docker
compose build app` 解决；仓库侧的根治方案见下方"compose/脚本修复"。

### 3. GPU vs CPU 资源策略：选择 GPU

VM 显存 23034 MiB，MonkeyOCR 常驻占用 16080 MiB，起始可用约 6.9GB。
按 spec 的"先试 GPU"策略，把 `docker-compose.standard.yml` 里 `tei-embed`
/`tei-rerank` 的镜像从 `cpu-latest` 换成 `ghcr.io/huggingface/text-embeddings-
inference:89-latest`（L4 是 Ada Lovelace，compute capability 8.9；TEI 按
compute capability 发布镜像 tag，不是裸版本号——8.9 用 `89-latest`），并
取消注释 `deploy.resources.reservations.devices`（nvidia GPU）段。

实测结果：两个 TEI 容器加载模型后，`nvidia-smi` 显示总占用 18835 MiB
（MonkeyOCR 16080 + TEI 两个约 2755 MiB），显存/RAM 都未告警、未触发
OOM/重启循环，**GPU 模式直接跑通，未降级**。RAM 方面 `docker compose build`
+ 4 容器运行时峰值约 6.4GB used / 15GB total，也有富余。

磁盘是本次最紧张的资源：初始 97GB 盘只剩 19GB 可用，装完 Docker
（约 1GB）+ 拉取 postgres/qdrant/TEI 镜像 + TEI 下载两个模型权重 + 构建
app 镜像后，一度降到约 3.9GB 可用（`docker system df` 显示 TEI 镜像本身
就占 8.85GB，含两份模型缓存卷 4.6GB）。清理 apt 缓存（`apt-get clean`，
约释放 500MB）后维持在 3.9GB 可用运行至今，未触发磁盘满。**若后续（H6）
还要拉更多镜像/模型，建议先规划磁盘或清理 MonkeyOCR 之外的无用文件。**

### 4. 渲染密钥、bring up

VM 侧 `~/kbase-standard/.env`（未提交仓库，仅记录已生成过这些值，具体
明文不写入本文档）：
- `POSTGRES_PASSWORD`：随机生成的 32 字节 urlsafe token
- `KBASE_SECRET_KEY`：随机生成的 32 字节 urlsafe token
- `KBASE_ADMIN_PASSWORD=KBaseAdmin@2026`
- `DASHSCOPE_API_KEY`：取自本地仓库 `.env`（已在 `.gitignore` 里，从未提交）

`config/kbase.standard.yaml` 的 `db.url` 占位符 `PASSWORD` 在 VM 上用
`sed` 替换成实际的 `POSTGRES_PASSWORD`（该文件本身在 VM 上是从
`git archive` 解出来的工作副本，不是仓库里的模板，仓库里的
`config/kbase.standard.yaml` 仍保持占位符原样，未提交明文密码）。

```bash
cd ~/kbase-standard
docker compose -f docker-compose.standard.yml up -d --build
```

4 个依赖服务（postgres/qdrant/tei-embed/tei-rerank）先后转 healthy，app
容器的 entrypoint.sh 等待逻辑正常工作，最终 app 也转 healthy。

### 5. 验证迁移 + 依赖连通性

```sql
-- docker exec kbase-standard-postgres-1 psql -U kbase -d kbase -c '\d chunks_kw'
Table "public.chunks_kw"
  Column  |         Type          | Nullable
----------+-----------------------+----------
 chunk_id | character varying(36) | not null
 kb_id    | character varying(36) | not null
 doc_id   | character varying(36) | not null
 tsv      | tsvector              |
Indexes:
    "chunks_kw_pkey" PRIMARY KEY, btree (chunk_id)
    "ix_chunks_kw_kb_id" btree (kb_id)
    "ix_chunks_kw_tsv" gin (tsv)          -- GIN 索引确认存在
```

`/healthz` 返回 `{"status":"ok","embedder":"TEIEmbedder","vectorstore":
"QdrantStore","reranker":"on"}`；容器内直接 curl `tei-embed:80/embed` 与
`tei-rerank:80/rerank` 均返回正确的推理结果（rerank 对语义相关文本给出
明显更高分）；qdrant `/collections` 可达。

### 6. `@pytest.mark.pg` 集成测试（首次真实验证）

在与 compose 同网络（`kbase-standard_default`）里跑一个临时容器（复用
`kbase:standard` 镜像 + 装 pytest），设置
`KBASE_TEST_PG_URL=postgresql+psycopg://kbase:<PASSWORD>@postgres:5432/kbase`：

```
tests/test_keyword_pg.py::test_index_search_roundtrip PASSED
tests/test_keyword_pg.py::test_kb_isolation PASSED
tests/test_keyword_pg.py::test_delete_doc_removes_only_that_doc PASSED
tests/test_keyword_pg.py::test_delete_kb_removes_all_rows PASSED
tests/test_keyword_pg.py::test_reindex_upsert_overwrites_same_chunk_id PASSED
================ 5 passed, 324 deselected, 3 warnings in 5.08s =================
```

`PGKeywordIndex` 此前 296+ 全绿套件里这 5 个用例一直被 deselected（本地无
PG），这是它们第一次真跑。全部通过。

### 7. 全链路冒烟

流程：登录 admin（`KBaseAdmin@2026`）→ 建 editor API Key → 建 KB → 传一份
中文 `.md`（兵团差旅费管理办法，含"住房补贴的申领条件为连续工作满两年"一句）
→ 轮询 `/api/kb/{id}/documents` 直到 `status=ready`（一次 poll 后即 ready，
说明摄取很快）→ `POST /api/kb/{id}/query` 问"住房补贴的申领条件是什么？"
→ 回答通过 SSE 流式返回，正确引用了文档并给出"连续工作满两年，且未享受
集资建房或房改房政策"→ `POST /api/kb/{id}/search debug=true` 显示 `dense`
/`keyword`/`fused`/`reranked` 四段 trace 均有值，证明 Qdrant 向量检索、PG
关键词检索、融合排序、TEI rerank 全部实际参与了这次检索。

冒烟用的 KB/文档/API Key 均保留在 VM 上（未清理），供 H6 复用或参考。

## compose/脚本修复（已提交）

- 新增 `.gitattributes`：`*.sh text eol=lf`，防止 Windows 端 `git archive`/
  checkout 再次把 shell 脚本转成 CRLF 导致容器 exec 失败。
- `docker-compose.standard.yml`：把 tei-embed/tei-rerank 的"H5 待验证"注释
  更新为实测结论（qdrant/TEI 镜像自带 bash，`/dev/tcp` healthcheck 按预期
  工作，不需要改探测方式），并把 GPU 部署注释更新为实测过的具体 tag
  （`89-latest`，对应 L4/Ada Lovelace compute capability 8.9）与实测显存
  数据。**镜像默认值仍保持 `cpu-latest`**（任何机器都能跑通的默认值不变，
  GPU 是部署时的显式选择，本次实际部署用的是取消注释后的 GPU 版本，VM 上的
  运行副本与仓库默认值不同，属预期行为）。

## 访问 / 运维一行流

```bash
GCLOUD="$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"

# SSH 到 VM
& $GCLOUD compute ssh instance-20260622-124009 --zone us-central1-b

# 通过 SSH 隧道从本地访问（本地 8100 转发到 VM 的 8100）
& $GCLOUD compute ssh instance-20260622-124009 --zone us-central1-b -- -L 8100:localhost:8100
# 之后本地访问 http://localhost:8100

# 查看栈状态 / 日志
cd ~/kbase-standard
docker compose -f docker-compose.standard.yml ps
docker compose -f docker-compose.standard.yml logs -f app

# 停止（不会删数据卷）
docker compose -f docker-compose.standard.yml stop

# 完全重新拉起
docker compose -f docker-compose.standard.yml up -d

# 彻底清理（含数据卷，谨慎）
docker compose -f docker-compose.standard.yml down -v
```

VM 上的实际路径：`~/kbase-standard/`（`git archive` 解出来的工作副本，
不是 git checkout，没有 `.git`）；`.env` 权限已设为 `600`。

## 资源快照（部署完成时）

- 显存：18835 / 23034 MiB 已用（MonkeyOCR 16080 + TEI×2 约 2755）
- 内存：约 6.4GB used / 15GB total，`available` 约 8.9GB
- 磁盘：3.9GB / 97GB 可用（**H6 若要继续拉镜像/加数据，需先规划**）

## 给 H6 的提示

- 栈已保持 UP，可直接在此基础上做负载测试。
- 磁盘只剩 3.9GB，是当前最大风险点——大批量文档导入 / 更多镜像拉取前
  建议先 `docker system df` 确认余量，必要时清理未用的 apt/pip 缓存或
  与用户确认是否需要扩盘（**不要擅自变更实例机型/磁盘，这需要停机**）。
- 如果 H6 的负载测试需要更多显存/更稳定的 GPU 独占，MonKeyOCR 与本栈
  GPU 共享是否需要临时让路，由 H6 视测试目标决定（当前 H5 全程未停
  MonkeyOCR）。
- 冒烟测试留下的 `h5-smoke-kb` 知识库 / `h5-smoke` API Key 可直接复用
  或清理，不影响其他数据。
