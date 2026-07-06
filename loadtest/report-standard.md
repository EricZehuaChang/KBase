# KBase standard 栈 100 并发压测报告（M4-2 Task H6）

- 日期：2026-07-06
- 分支：`feature/m4-2`
- 目标机器：`instance-20260622-124009`（zone `us-central1-b`，g2-standard-4，
  NVIDIA L4 / 23034 MiB 显存），与常驻 MonkeyOCR（uvicorn :7861，~16GB 显存）
  共存。全程未停止/重启 VM 实例、未改动实例机型。
- 验收线（spec §11）：`POST /api/kb/{id}/search`（TEI-embed 向量化 + Qdrant
  稠密检索 + PG 关键词检索 + RRF 融合 + TEI-rerank 重排，不含 LLM 生成）
  100 并发下 P95 < 500ms，错误率 0。

## 结论摘要（如实报告，未达标）

**未达标。** 100 并发下检索 P95 实测 4.7s～9.3s（视调优前后而定），是
500ms 验收线的 9～18 倍。瓶颈是 L4 单卡的 TEI-rerank 推理吞吐，不是
uvicorn/DB 连接池，也不是与 MonkeyOCR 的显存/进程争抢——详见下文分析。

压测过程中发现一个比"P95 超标"更重要的问题：调优前的压测数字表面上
"0 HTTP 错误、RPS 尚可"，但那是 TEI-rerank 内部过载后用 429 拒绝了
**71%** 的重排请求，被检索器的降级容错（rerank 失败时静默退化为融合排序，
不重排但不报错）悄悄吞掉——对压测客户端而言这确实是"0 错误"，但对最终
用户而言，大部分请求实际上没有被重排，是结果质量的隐性劣化，不是真正的
"系统在 0 错误下扛住了 100 并发"。调优（提高 TEI-rerank
`max-concurrent-requests`）之后，429/降级归零，所有请求都被真实重排，
但代价是排队时间转移到了看得见的响应延迟里——P95 从约 4.7s 涨到约 9.3s。
两组数字都在下表如实列出。

## 环境

| 项 | 值 |
|---|---|
| VM | g2-standard-4，NVIDIA L4（23034 MiB），Ubuntu，97GB 盘 |
| 栈 | docker-compose.standard.yml：app（uvicorn 单进程）+ postgres:16-alpine + qdrant + tei-embed(bge-m3, GPU) + tei-rerank(bge-reranker-v2-m3, GPU) |
| 共存服务 | MonkeyOCR（uvicorn :7861，独立进程，~16GB 显存），全程未停止（除隔离测试环节，见下） |
| 语料库 | 专建 `loadtest-kb`：40 篇合成中文政策类 .md（差旅/住房/绩效/培训/合同/社保/安全生产等 30 个主题域，varied headings + paragraphs），PG `chunks` 表 663 行，Qdrant 与 PG 关键词索引各 357 个可检索单元（详见"语料与已知数据面问题"） |
| 压测工具 | locust 2.44.4，独立 venv（`~/loadtest-venv`），**在 VM 本机运行**（非经 SSH 隧道），取纯服务端口径 |
| 压测口径 | `POST /api/kb/{kb_id}/search`，`Authorization: Bearer <apikey>`，20 条真实中文问句池随机轮换，`top_k=5`，`debug=false` |
| 鉴权 | 独立 `loadtest-key`（editor 角色），与 H5 遗留的 `h5-smoke-kb`/`h5-smoke` key 互不干扰 |

## 语料与已知数据面问题（诚实记录，已更正）

种入 40 篇文档、PG `chunks`（文档分块元数据）表有 663 行，但 Qdrant 与
PG 关键词索引（`chunks_kw`）各只有 357 个条目。**本报告曾在此处记录一个
错误假设**："首批 40 个文件并发上传时命中过一次 Qdrant 集合已存在 409
竞态，导致 chunks 元数据表留下未清理的重复行"——commit `e236c9d`
（`test: 记录 H6 摄取计数差异为预期设计（父块+叶子块），非数据一致性
bug`）已用真实数据核实并推翻这个猜测：40 个文档全部 `status=ready`，
`content_hash` 无重复组，`chunks` 与 `chunks_kw` 都完整覆盖全部 40 个
`doc_id`，不存在"块已写入但向量/关键词索引丢失"的竞态。

真正原因是架构设计使然：`chunks` 表按设计同时存父块（章节，
`is_leaf=False`，仅供上下文组装用，不进向量/关键词索引）与叶子块
（`is_leaf=True`，唯一被向量化/关键词索引的对象）——663 = 306 父块 + 357
叶子块，与 40 个文档各自 chunker 输出一一对应，是预期行为而非数据一致性
bug。新增的 `tests/test_ingest.py::test_ingest_duplicate_content_docs_dedup_to_one_and_chunk_counts_stay_consistent`
最小复现了这个父块:叶子块比例关系。**这不影响压测的有效性**——357 个
可检索单元仍是一个有意义、非空的语料规模，检索链路的四段 trace
（dense/keyword/fused/reranked）在冒烟测试中全部正常产生候选，搜索接口在
整个压测过程中零 HTTP 500。

另外验证发现（非本次压测目标，但检索质量相关的诚实记录）：PG 关键词路
用 `plainto_tsquery('simple', ...)` 对分词后的整句做 AND 语义连接，长
自然语言问句（含"的/是/什么"等虚词）在关键词路容易 0 命中（本次冒烟
用例"住房补贴的申领条件是什么"关键词路 trace 为空，而单独查"住房补贴"
能命中 14 个块）——这是既有关键词检索层的固有行为，不是本次种入语料或
本次改动引入的问题，融合排序靠稠密路 fallback 未受影响。

## 压测结果

### 调优前（TEI-rerank `max-concurrent-requests` = 默认 512）

| 并发 | P50 | P95 | P99 | RPS | 错误数 | 备注 |
|---|---|---|---|---|---|---|
| 10 | 800ms | 1100ms | 1100ms | 11.8 | 0 | 未触发降级 |
| 50 | 670ms | ~3000ms | ~3300ms | 31.0 | 0 | 已观察到 TEI-rerank 429（未精确计数） |
| 100 | 2300ms | ~4700ms | ~5000ms | 30.9 | 0 | **2105/2947 请求（71%）触发 429→降级为融合排序，未真正重排** |

100 并发下，MonkeyOCR 隔离对照实验（临时停止 MonkeyOCR，释放约 16GB
显存，仅剩 TEI×2 独占 GPU）**数字几乎不变**（P50 2300ms、P95 ~4800ms、
RPS 31.0）——证明瓶颈不是与 MonkeyOCR 的显存/算力争抢，MonkeyOCR 停止
后按流程重启恢复。

### 调优后（TEI-rerank `max-concurrent-requests` = 2048，DB 连接池
pool_size=50+max_overflow=50）

| 并发 | P50 | P95 | P99 | RPS | 错误数 | 备注 |
|---|---|---|---|---|---|---|
| 10 | 800ms | 1100ms | 1100ms | 11.8 | 0 | 与调优前一致（未触发降级/排队） |
| 50 | 4100ms | 4300ms | 4400ms | 12.1 | 0 | 429/降级归零，全部请求真实重排 |
| 100 | 8300ms | 9250ms | 8700ms（P99，见原始 CSV） | 11.9 | 0 | 429/降级归零，全部请求真实重排；TEI-rerank 单次 rerank 调用 `queue_time` 约 1.7～2.8s，`inference_time` 仍稳定在约 260ms |

原始 CSV：`loadtest/results/run_10_stats.csv`、`run_50_stats.csv`、
`run_100_stats.csv`（调优后最终状态，已从 VM 取回存档于本目录）。

## 根因分析

1. **不是 uvicorn 单进程/线程池**：曾假设 100 并发下 `run_in_threadpool`
   默认线程池 + SQLAlchemy 默认连接池（pool_size=5+max_overflow=10）是
   瓶颈，把 PG 连接池调到 50+50（`kbase/db.py`）后重跑 100 并发，数字
   几乎不变（P50 2200ms vs 调优前 2300ms）——证伪。
2. **不是与 MonkeyOCR 的资源争抢**：临时停止 MonkeyOCR 后重跑，数字
   几乎不变——证伪。
3. **是 TEI-rerank 的 GPU 推理吞吐**：`docker logs kbase-standard-tei-rerank-1`
   显示 100 并发下单次 `rerank` 调用的 `inference_time` 稳定在约
   260ms（无论调优前后），但 `total_time` 在调优前后分别达到约
   140ms～2.4s（未打满 permit 时快速失败）和约 3s（打满后真实排队）。
   每次 `/api/kb/{id}/search` 请求携带 `candidates=20` 个候选块送入
   TEI-rerank 交叉编码器打分，100 个并发请求意味着 2000 个候选文本对
   同时排队等这块 L4 GPU 的算力——单卡打分吞吐（每次约 260ms，无论批多大
   队列多深，物理算力总量不变）无法在 500ms 内消化这个量级的重排请求，
   这是硬件算力天花板，不是配置问题。
4. **一个应用层的隐性行为，比延迟数字本身更值得关注**：TEI 在自身并发
   请求数超过 `max-concurrent-requests` 时不排队而是快速返回 429
   （"no permits available"）；`kbase/rag/retriever.py` 里 rerank 调用
   失败会静默降级为融合排序（这个设计初衷是"重排服务瞬时不可达时不让
   整个查询挂掉"，对偶发故障是合理的容错），但在持续过载场景下，这个
   容错路径变成了一个隐性的"自动降级到更差但更快的排序"开关，在压测
   数字上表现为迷惑性的"0 错误、尚可的 RPS"。把 `max-concurrent-requests`
   调高后，429/降级归零，才看到真实的排队延迟。**这是本次压测最重要的
   发现**：不看 429/降级计数、只看 HTTP 错误率的验收方法本身会漏掉这类
   隐性质量劣化，建议后续把"reranked 降级发生率"也纳入生产监控指标。

## 已尝试的调优（按 spec §4 顺序）

| 旋钮 | 改动 | 100 并发结果 | 是否见效 |
|---|---|---|---|
| DB 连接池 | `kbase/db.py`：PG 方言下 `pool_size=50, max_overflow=50, pool_pre_ping=True`（原为 SQLAlchemy 默认 5+10） | P50 2200ms（调优前 2300ms），几乎无差异 | 否——证明瓶颈不在这里，但作为通用生产加固保留 |
| MonkeyOCR 隔离对照 | 临时停止 MonkeyOCR 进程，压测后按 `~/start_api.sh` 重启恢复 | P50/P95 几乎不变 | 否——排除 GPU 多租户争抢假设 |
| TEI 并发参数 | `docker-compose.standard.yml`：`tei-rerank` 增加 `--max-concurrent-requests 2048`（原默认 512） | 429/降级归零，但 P95 从约 4.7s 涨到约 9.3s（排队显性化） | **方向上"更诚实"，但不解决 P95 超标**——只是把隐性质量劣化换成了显性高延迟 |
| uvicorn workers | 未改（entrypoint.sh 仍单进程） | — | 未尝试：诊断已证明瓶颈是 TEI-rerank GPU 侧，多进程不会增加 GPU 算力，预期无效，为聚焦关键问题未做无意义改动 |
| Qdrant 检索参数 | 未改 | — | 未尝试：Qdrant 侧从未成为瓶颈（qdrant 容器 CPU 占用全程 <5%），无需调 |

**最终保留状态**：DB 连接池调优保留（无害且是合理的生产加固）；
TEI-rerank `max-concurrent-requests=2048` 保留（消除隐性降级，让系统
在过载时诚实地变慢而不是诚实地"看起来还行但没真正工作"，更适合作为
生产默认行为；如果业务更看重"宁可结果质量下降也要快速响应"，可以把
这个值调回默认或更低，这是一个可以讨论的产品取舍，本报告不替业务做
决定，只保证配置的行为是可预期、可解释的）。

## SSE 问答冒烟（5 次顺序调用，非负载测试）

| # | 问题 | 结果 | 耗时 |
|---|---|---|---|
| 1 | 住房补贴的申领条件是什么 | 正常回答+引用[1][2][4][5] | 12.5s |
| 2 | 差旅费报销标准是多少 | 正常回答（语料未含具体数值，如实说明未找到标准要素） | 11.9s |
| 3 | 年度带薪休假天数如何计算 | 正常回答（如实说明语料无计算规则） | 10.5s |
| 4 | 绩效考核分为哪几个等级 | 正确拒答（语料确实未定义等级方案，检索无依据） | 0.13s |
| 5 | 劳动合同解除需要什么条件 | 正常回答+引用 | 10.8s |

5/5 全部收到 `event: done`，0 个 HTTP/流错误，DashScope 从 VM 可正常
访问（原计划里担心的 IPv6/网络问题未出现）。生成路径未见回归，与 H5
冒烟结论一致。查询 4 的"拒答"是正确行为而非 bug——语料确实不含考核
等级枚举，检索器没有编造依据。

## 资源快照

| 项 | 压测前 | 压测后 |
|---|---|---|
| 磁盘 | 3.9GB 可用 / 97GB | 3.8GB 可用（一度因调优过程中的操作失误降到 2.9GB，已清理恢复，见"过程中的插曲"） |
| 显存 | 18835 MiB / 23034 MiB（MonkeyOCR 16080 + TEI×2 约 2755） | 16265～16713 MiB / 23034 MiB（MonkeyOCR 重启后 ~16265 常驻，压测中 TEI×2 短暂占用另加约 450～1300 MiB） |
| GPU 利用率 | 空闲 0% | 100 并发压测期间稳定 99～100% |

## 过程中的插曲（诚实记录一次操作失误）

调优 TEI-rerank 并发参数时，把本地仓库里 `docker-compose.standard.yml`
的**默认值版本**（`image: ...cpu-latest`，注释掉 GPU `deploy` 段——这是
H5 特意保留的"任何机器都能跑通"的可移植默认值）覆盖 scp 到了 VM 的
工作副本，导致 `docker compose up -d tei-rerank` 短暂把 tei-rerank
从 GPU 切到了 CPU 镜像，触发一轮 crash-loop（ONNX 模型文件缺失
+ healthcheck 未通过反复重启，7 次重启）并多拉了一份约 1GB 的 CPU 镜像
（磁盘一度降到 2.9GB）。发现后立即修复：直接在 VM 工作副本上把
`tei-rerank` 镜像改回 `89-latest` 并恢复 GPU `deploy` 段（不改仓库
默认值，遵循 H5 记录的"仓库默认值=CPU 可移植、VM 部署=GPU 覆盖，两者
有意分离"的既有约定），重新 `up -d` 后确认 `Starting FlashBert model
on Cuda`、healthy、restarts=0，随后删除了多余的 cpu-latest 镜像回收
磁盘。全程未影响 VM 实例本身、未影响 MonkeyOCR、未影响其他压测数据的
有效性（受影响的仅是这一次 `up -d tei-rerank` 到修复之间约 5 分钟窗口，
未落入任何一组正式压测数据）。记录此事是为了如实说明压测过程，而不是
因为它改变了最终结论。

## 验收结论

**不达标。** 100 并发下 `POST /api/kb/{id}/search` P95（调优后）约
9.25s，是 500ms 验收线的约 18.5 倍；错误率确为 0（HTTP 层面），但调优前
的"0 错误"里 71% 请求经历了未在错误率里体现的重排降级，这一点必须在
验收结论里同时说明，否则单独一个"错误率 0"的表述会误导。

根因是单张 L4 GPU 承载 TEI-embed + TEI-rerank + MonkeyOCR 三个模型服务
时，TEI-rerank 的交叉编码器推理吞吐（每次调用約 260ms，与批大小/并发数
无关，是模型计算量决定的物理下限）无法满足 100 并发×20 候选/请求这个
量级的重排需求。这不是一个可以靠 uvicorn/连接池/Qdrant 参数调优解决的
问题——已按 spec §4 顺序验证了这些旋钮均无效或方向错误。真正能解决的
方向（供决策参考，本报告不擅自实施，因为都超出"调优现有部署"的范围）：
更大/独立的 GPU 用于 TEI-rerank（不与 MonkeyOCR 共享）、减少每次检索的
`candidates` 候选数（当前 20，是应用层的检索质量参数而非部署参数，
减少会牺牲召回质量）、或在极高并发场景下取消强制重排、仅在低并发时
启用（产品策略问题）。这些是留给用户/后续里程碑决策的选项，如实列出
而非替用户做主。

**以上是 H6 任务的结论，下一节 H6.5 在此基础上实施了其中一个方向
（"极高并发场景下取消强制重排"，做成运行时自适应而非全局静态开关）
并重新压测，见下文。**

---

# H6.5：重排过载自适应降级 —— 复测报告

- 日期：2026-07-06（同一 VM，紧接 H6 之后）
- 分支：`feature/m4-2`，commit 见本次提交历史（`feat: 重排过载自适应降级`）
- 改动摘要：`kbase/rag/retriever.py` 给 Retriever 加一个
  `threading.BoundedSemaphore(max_concurrency)`（默认 8，
  `kbase/config.py` `RerankConfig.max_concurrency` 可配置）。重排调用前
  `sem.acquire(blocking=False)`：抢到才真正调用 TEI-rerank（异常仍走 H2
  既有的降级为融合排序路径），抢不到**直接跳过这次重排调用本身**（非阻塞
  跳过，不是"调用后丢弃结果"），立即降级为融合排序。`rerank_status` 四态
  （on/shed_load/error/off）写进 debug trace 与 `/healthz` 的
  `rerank_stats`（`rerank_total`/`rerank_shed_load_total`/
  `rerank_error_total` 三个计数器），让降级从"静默"变成"可观测"。

## 部署与环境说明（诚实记录一个环境漂移）

把 H6.5 代码同步到 VM 时发现：`~/kbase-standard` 工作副本的
`docker-compose.standard.yml` 里 `tei-embed` 服务在本次操作开始前就已经
处于 `cpu-latest` 镜像 + GPU `deploy` 段被注释掉的状态（文件修改时间早于
本次会话开始时间），与 H5/H6 报告记录的"GPU 模式两个 TEI 都跑通"不一致
——这是本次改动之外的环境漂移（推测是介于 H6 收尾与本次开始之间，工作
副本被某次操作意外改回了仓库默认值），不是本次代码同步引入的问题（本次
同步刻意排除了 `docker-compose.standard.yml`，只覆盖了 `kbase/`、
`tests/`、`config/kbase.standard.yaml` 等应用代码文件）。为了让本次复测
数字与 H6 基线可比（CPU embed 远慢于 GPU embed，会混淆重排优化的效果判断），
已把 `tei-embed` 改回 `89-latest` GPU 镜像并取消注释 GPU deploy 段，
`docker compose up -d tei-embed` 后确认 healthy，`nvidia-smi` 显存占用
16297 MiB，与 H6 记录的量级一致。

修复过程中一度触发磁盘紧张（GPU 版模型权重重新拉取/缓存，可用磁盘从
3.8GB 降到 742MB，97GB 盘 100% 满）——已清理不再使用的 `cpu-latest`
tei-embed 镜像（938MB）恢复到约 1.6～1.7GB 可用，压测期间及压测后磁盘
稳定，未再恶化。`tei-rerank` 的 `max-concurrent-requests=2048`（H6 遗留
调优）保持不变。

## 压测结果（`max_concurrency=8`，与 H6 完全相同的 locust 梯度/参数/语料/Key 角色）

| 并发 | P50 | P95 | P99 | RPS | 错误 | shed-rate（本级增量） |
|---|---|---|---|---|---|---|
| 10 | 110ms | 860ms | 980ms | 27.5 | 0 | 1515/2462 = 61.5% |
| 50 | 1600ms | 2500ms | 2800ms | 28.9 | 0 | 1658/2608 = 63.6% |
| 100 | 3300ms | 4200ms | 4500ms | 28.8 | 1（0.04%，与重排无关，见下） | 1709/2685 = 63.6% |

原始 CSV：`loadtest/results/adaptive_run_{10,50,100}_stats.csv`（本次复测，
已从 VM 取回存档于本目录）。100 并发下唯一 1 次失败，app 容器日志显示：

```
httpcore.RemoteProtocolError: Server disconnected without sending a response.
qdrant_client.http.exceptions.ResponseHandlingException: Server disconnected without sending a response.
```

发生在稠密召回阶段（Qdrant HTTP 客户端），在重排分支**之前**，与本次
改动无关——是高并发下 Qdrant 连接的既有瞬时抖动（H6 报告记录 qdrant 容器
CPU 全程 <5%，本次也未复查 Qdrant 侧指标，暂按既有瞬时抖动记录，不排除
后续需要专项复现）。

**与 H6 基线对比（100 并发）**：P95 从 9250ms 降到 4200ms，约 **2.2 倍
改善**；50 并发 P95 从 4300ms 降到 2500ms；10 并发基本不变（未接近
`max_concurrency=8` 的容量上限，几乎不触发 shed，H6 报告 10 并发也未触发
降级，量级一致）。**但仍未达到 500ms 验收线。**

## 关键根因排查：为什么 P95 仍未达标（诚实记录，超出本任务原定范围的新发现）

用 `docker compose logs tei-rerank` 核实 100 并发压测窗口内单次 rerank
调用的真实耗时：`total_time≈550-850ms`（`queue_time≈300-400ms` +
`inference_time≈170-260ms`）——**相比 H6 基线的 `queue_time` 1.7～2.8s
已经大幅缩短**，证明本次的有界并发信号量确实把 TEI 侧排队压下来了。
`tei-embed` 日志确认 embed 本身很快（`total_time` 10～20ms 量级），
不是新瓶颈。

但 locust 观测到的整体 P50/P95（3.3s/4.2s）仍远高于"idle 单请求全链路
（含真实重排）only ~110ms"这个基线。用
`anyio.to_thread.current_default_thread_limiter().total_tokens` 在本地
`.venv` 核实：**Starlette/AnyIO 的 `run_in_threadpool` 默认线程池容量是
40**，`kbase/api/main.py` 调用 `run_in_threadpool(retriever.retrieve, ...)`
时没有传自定义 limiter，用的就是这个默认值。100 并发请求下，只有 40 个
能同时真正执行 `retrieve()`（embed+dense+keyword+DB 组装全部在内），
其余 60 个请求排队等线程池槽位——这是一个与本次重排信号量**完全独立**
的并发上限，且发生在请求路径更早的阶段（先拿到线程槽位，才谈得上本次
新增的重排信号量判断）。这足以解释"TEI 侧排队已经从 2.8s 压到 0.3～0.4s"
之后，为什么整体 P95 仍停留在秒级而非毫秒级——线程池本身的排队接管成为
新的主导延迟来源。

这是一个诚实的、超出本任务原定范围（"重排过载自适应降级"）的新发现：
本次的信号量按设计正确解决了它要解决的问题（重排 GPU 过载导致的排队），
但同时暴露了下一层瓶颈（uvicorn/AnyIO 默认线程池容量 40）。是否顺手调大
线程池容量留给后续任务决策——调大会影响所有走 `run_in_threadpool` 的
端点而不只是 search，需要独立评估（例如与 DB 连接池、CPU 核数匹配），
不在本次改动范围内。

## `max_concurrency` 敏感性测试（4 vs 8）

在 100 并发下把 `config/kbase.standard.yaml` 的 `max_concurrency` 从 8
临时改成 4（`docker compose restart app` 生效，无需重新 build），重跑
同一个 100 并发 locust 用例：

| max_concurrency | P50 | P95 | P99 | RPS | 错误 | shed-rate |
|---|---|---|---|---|---|---|
| 8 | 3300ms | 4200ms | 4500ms | 28.8 | 1（Qdrant 瞬时抖动，见上） | 63.6% |
| 4 | 3400ms | 4100ms | 4400ms | 28.6 | 0 | 63.9% |

原始 CSV：`loadtest/results/adaptive_mc4_run_100_stats.csv`。**两组数字
在统计噪声范围内基本一致**——这与上一节的根因分析吻合：当前 100 并发下
的主导瓶颈已经是 AnyIO 线程池容量（40），不再是 TEI-rerank GPU 排队，
所以把 `max_concurrency` 从 8 进一步降到 4 几乎不影响整体 P50/P95（TEI
侧排队本来就已经很短，降低信号量容量只是让 shed-rate 从 63.6% 略升到
63.9%，但线程池瓶颈原样存在）。测试完成后已把 `max_concurrency` 恢复为
默认值 8（VM 与仓库 `config/kbase.standard.yaml` 一致）。

`max_concurrency=8` 在正常负载（10 并发）下几乎不引入额外 shed（H6 报告
10 并发从未触发降级），是一个合理的默认值；本次复测显示 8 与 4 在 100
并发下表现相近，**没有证据支持需要比 8 更低**——真正决定性的旋钮是线程
池容量而非这个信号量，因此保留 8 作为默认，不建议仅为压测数字盲目调低。

## 100/50/10 三级 shed-rate 与 rerank_stats 摘要

`/healthz` 的 `rerank_stats` 在每级压测前后快照（数值为累计计数器）：

| 阶段 | rerank_total | rerank_shed_load_total | rerank_error_total |
|---|---|---|---|
| 10 并发前 | 1 | 0 | 0 |
| 10 并发后 | 2463 | 1515 | 0 |
| 50 并发前 | 2471 | 1515 | 0 |
| 50 并发后 | 5079 | 3173 | 0 |
| 100 并发前 | 5116 | 3197 | 0 |
| 100 并发后 | 7801 | 4906 | 0 |

`rerank_error_total` 全程为 0：本次压测期间 TEI-rerank 本身未出现异常/
超时（有信号量兜底容量，未打满 TEI 自身的 `max-concurrent-requests=2048`
上限），H2 的"异常降级"路径未被触发，触发的是本次新增的"容量降级"
（`shed_load`），两者在 trace/计数器里清晰区分，符合设计目标。

## 验收结论（H6.5，更新版）

**仍不达标，但有实质性改善。** 100 并发下 `POST /api/kb/{id}/search`
P95 约 4.2s，是 500ms 验收线的约 8.4 倍——相比 H6 基线的 9.25s（约 18.5
倍）**改善约 2.2 倍**，其中约 63.6% 的查询在这一级被主动降级为融合排序
（诚实、可观测，通过 `/healthz.rerank_stats` 和 debug trace 的
`rerank_status` 字段暴露，不是像 H6 报告记录的 429 静默降级那样不可见）。

全精排"舒适区"（几乎不触发 shed_load 的并发档位）大致在个位数到 10
并发以下——10 并发已经触发 61.5% 的 shed-rate，说明 `max_concurrency=8`
在请求到达速率明显高于"8 个并发重排调用能在合理时间内清空"时就会开始
降级；这与 locust `wait_time=between(0,0)`（无思考时间的纯并发轰炸）
口径有关，真实产品流量的请求到达模式通常更稀疏，实际 shed-rate 会低于
本报告的压测数字。

根因分两层：第一层（TEI-rerank GPU 排队）已被本次信号量机制有效缓解
（`queue_time` 从 H6 的 1.7～2.8s 降到 0.3～0.4s）；第二层（AnyIO 默认
线程池容量 40）是本次诊断中新发现的、独立于重排的并发上限，是当前 100
并发下 P95 无法进一步下降到 500ms 量级的主要原因，**不在本次任务范围内
修复**（改动会影响所有 API 端点，需要独立评估线程池大小与 DB 连接池/
CPU 核数的匹配关系）。`max_concurrency` 从 8 降到 4 对 P95 影响甚微
（在统计噪声范围内），进一步印证第二层瓶颈已经成为主导因素。

如实总结：本次改动实现了任务要求的目标——把"100 并发全部排队等 GPU"
转变为"部分查询降级为快速融合排序、其余享受真实重排"，降级从静默变为
可观测；P95 数字确有大幅改善但未达标，诚实报告实际数字与新发现的第二层
瓶颈，供后续里程碑决策是否需要调大线程池容量或采用其他架构方案（如
独立 GPU、减少 candidates、请求排队+背压等）。

## 线程池调优后（M4-2 Task H7 Step 0，如实报告：未带来改善，反而略有回退）

H6.5 提出的假设是：AnyIO 默认线程池容量 40 是 100 并发下的新主导瓶颈
（TEI-rerank 侧排队已被信号量压到 0.3～0.4s 之后暴露出来的下一层）。本次
新增 `server.threadpool_size` 配置项（默认 40，不配置=零行为变化；
`AppConfig` 新增 `ServerConfig`，见 `kbase/config.py`），在 FastAPI
startup 钩子里设置 `anyio.to_thread.current_default_thread_limiter().
total_tokens`——`create_app()` 本身是同步函数，调用时还没有运行中的事件
循环，直接在 `create_app` 里调用 `current_default_thread_limiter()` 会
抛 `NoEventLoopError`（本地脚本验证过），必须挪到 startup 钩子（`app.
on_event("startup")`，FastAPI 新版建议用 `lifespan=` 但 `on_event` 仍可用
且更不侵入现有 `create_app` 结构，这里选择保留 `on_event`）。standard
profile 的 `config/kbase.standard.yaml` 设置为 120。

部署方式：VM 磁盘持续处于约 1.6GB 可用的紧张状态（`docker builder prune
-f` 已无可回收空间），未做镜像重建——采用热补丁：`docker cp` 把改动后的
`kbase/api/main.py`、`kbase/config.py` 覆盖进运行中的 `kbase-standard-app-1`
容器文件系统，`config/kbase.standard.yaml` 本身是 bind mount，直接改宿主机
文件即可生效，然后 `docker compose restart app`（不重建镜像，磁盘占用
全程未变化，压测前后均为 1.6GB 可用）。

**验证线程池设置确实生效**：热补丁后一度因为 VM 工作副本的
`config/kbase.standard.yaml` 里 `db.url` 密码占位符被本次 scp 覆盖回仓库
的字面 `PASSWORD` 占位符而导致 app 容器连接 PG 失败崩溃重启——这是本次
操作引入的部署事故（覆盖文件时没意识到 VM 工作副本上该文件的密码字段
已被手工渲染成真实密码），从 VM 的 `.env` 里取回 `POSTGRES_PASSWORD` 重新
替换后恢复正常，记录在此警示后续同类热补丁操作。功能验证：用
`docker exec` 起一个新的 Python 子进程读取同一份 config 确认
`threadpool_size` 解析为 120（配置层没问题）；用 100 并发压测期间的
`/proc/1/status`（PID 1 即容器内的 uvicorn 主进程）采样确认线程数从空闲时
的 9 涨到压测中的 108（超过旧的 40 上限，说明 limiter 确实在生效，
run_in_threadpool 不再卡在 40 个槽位）。

**100 并发压测结果（与之前完全相同的 locust 参数/语料/Key/kb，三次独立
运行取得一致结论）**：

| 配置 | P50 | P95 | P99 | RPS | 错误 |
|---|---|---|---|---|---|
| threadpool=40（对照组，本次重新压测） | 3300ms | 4200ms | 4400ms | 28.7 | 0 |
| threadpool=120（第一次） | 3400ms | 5100ms | 6000ms | 28.1 | 0 |
| threadpool=120（重跑复现） | 3500ms | 5100ms | 5900ms | 27.9 | 0 |
| threadpool=120（最终存档版，CSV 见下） | 3500ms | 5200ms | 5900ms | 27.7 | 0 |

原始 CSV：`loadtest/results/threadpool120_run_100_stats.csv`（threadpool=120
最终版）、`loadtest/results/threadpool_control40_run_100_stats.csv`
（threadpool=40 对照组，本次重新压测，与 H6.5 报告记录的 P95 4.2s 一致）。

50 并发：threadpool=120 时 P50=1700ms/P95=2600ms（`loadtest/results/
threadpool120_run_50_stats.csv`），与 H6.5 报告的 threadpool 调优前数字
（P50=1600ms/P95=2500ms）基本一致，在噪声范围内。

shed-rate 在三次 100 并发运行中稳定在 62.4%～63.2%，50 并发稳定在
63.0%～63.6%——与 H6.5 报告记录的比例一致，符合预期（shed-rate 由
`retrieval.rerank.max_concurrency` 这个独立信号量决定，与线程池容量无关，
调线程池不应该也确实没有影响它）。

**结论：threadpool_size 从 40 提到 120 没有带来 P95 改善，100 并发下反而
从 4.2s 小幅退步到约 5.1～5.2s（约 20～25%），50 并发基本无变化。** 这与
H6.5 报告提出的假设方向相反——诚实记录并给出最可能的根因：

本次压测所用的 GCP VM（`g2-standard-4`）只有 **4 个 vCPU**（`nproc` 确认，
容器内外一致）。H6.5 的假设隐含前提是"线程池槽位不够、CPU 有富余"，但
实测 100 并发下 `docker stats` 显示 `app` 容器 CPU 占用在 threadpool=40
时已达 97.75%（接近打满其可分配份额），threadpool=120 时反而降到
69～76%——这个现象本身就说明真正的限制因素是 4 个物理核心的可调度总量，
不是线程槽位数：线程池从 40 提到 120 后，Python GIL 之下可同时"就绪"的
线程更多，但 4 个核心的调度器需要在更多线程间做上下文切换（每个线程仍要
排队等 GIL 和等 CPU 核心，只是排队的位置从"等线程池槽位"变成了"等 GIL/
等核心"），额外的调度开销让尾延迟略微变差，而不是像 H6.5 假设的那样
"释放被压抑的并行度"——因为可用并行度从一开始就只有 4 份，40 个线程已经
远超 4 核心能真正同时服务的数量，槽位从 40 加到 120 对"能同时跑的请求数"
没有实质提升，只是多了 80 个线程在排队。

这是一个比"改动没用"更值钱的诚实发现：H6.5 报告里"线程池容量是人为
上限"的判断本身没错（40 确实是一个可配置但此前从未暴露的上限），但
"调大它就能提升 P95"这个推论建立在"CPU 有富余、线程只是在空等槽位"
这个未经验证的假设上——本次实测直接证伪了这个假设：瓶颈已经从"线程池
槽位数"下沉到"物理核心数"，在只有 4 vCPU 的机器上，这一层已经没有更多
"零成本"的余量可榨，调大 `threadpool_size` 更多是在与 CPU 调度器的
开销做权衡，而不是解锁真实并行度。`server.threadpool_size` 配置项本身
按设计正确工作（已用单元测试 + 生产环境双重验证），保留在 standard
profile 里体现"这是一个可调旋钮"的意图，但把它设为 120 在当前这台
4-vCPU 的 VM 上不是有效的性能手段——更大的 CPU 配额（更多 vCPU）或者
把 embed/keyword/DB 组装这些 CPU-bound 工作进一步优化/并行化才是下一步
真正有希望的方向，不是继续加大线程池容量。
