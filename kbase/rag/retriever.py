"""检索器：分级管道 —— 双路召回（稠密+关键词）-> RRF 融合 -> (重排，A4 接线) -> 父块组装。

Chroma 中只索引叶子块向量；命中叶子后回 SQLite 取其父块全文作为上下文，
同一父块下的多个叶子命中去重，只返回一次（small-to-big）。

关键词路与稠密路的分数不可比（bm25 变体 vs 余弦），融合一律走 RRF 按名次，
不直接加权分数。关键词路独有的候选没有稠密分数，为了让下游（阈值/展示）
拿到统一语义的余弦，用 ChromaStore 的存量向量补算。

重排中途失败降级：self._reranker.rerank() 是一次运行时网络调用（TEI 服务），
查询期间可能瞬时不可达/超时。若不接住，异常会一路冒泡穿出
run_in_threadpool，最终在 /api/kb/{id}/query 与 /api/kb/{id}/search 两个
路由变成未处理异常 -> FastAPI 默认 500。这里改为捕获异常、记录 warning，
本次查询降级为融合排序（即 reranker=None 时走的同一条 cosine-backfilled
fused 顺序分支，见 _ordered_without_rerank）。降级后 trace 里没有
"reranked" 键，但仍有 dense/keyword/fused（debug=True 时可观测到降级）。
遗留影响：降级后 blocks 携带的是余弦分数而不是重排分数，而生成器的
min_score 是按「重排是否生效」这个应用启动时刻的静态决策分模式选定的
（rerank-active 模式下用 min_score_rerank=0.35）。单次查询临时降级为余弦
分数后仍然拿去跟 0.35 这个"重排量纲"的阈值比较——这在语义上不精确，但
0.35 作为余弦阈值反而更严格（合法的、偏保守的稠密门槛），不会导致误放行
噪声块；因此这里不为这一次性降级去反查/重新配置每次查询的阈值模式，
按已有 min_score_rerank 门槛处理即可（见 kbase/rag/generator.py 及
应用启动时按 rerank.enabled 选择 min_score 的逻辑）。

重排过载自适应降级（M4-2 H6.5）：H6 压测证明单卡 L4 上 TEI-rerank 的交叉
编码器推理吞吐是固定物理上限（每次约 260ms，与批大小/并发无关），100
并发同时把 20 候选/请求送进去重排会导致所有请求排队等这一张卡，P95 被
拖到 9s+。这里不是"重排偶发故障"（上面那段注释的场景），而是"重排服务本身
正常但吞吐跟不上并发量"——需要一种不同的应对：不能傻等（排队本身就是
P95 灾难的来源），而应该主动拒绝超额的重排请求、让多余的查询立刻走 fused
降级（比等 9s 拿到一个重排结果更符合大多数产品的延迟预算）。
做法：self._rerank_sem 是一个容量为 max_concurrency 的
threading.BoundedSemaphore，在 Retriever 构造时创建一次（Retriever 实例
被所有请求共享，每个 retrieve() 调用跑在 run_in_threadpool 分配的独立
线程里——是同步阻塞代码，线程数量不固定且可能远超 max_concurrency，所以
用 threading 而非 asyncio 的信号量）。每次进重排分支前先
sem.acquire(blocking=False)：抢到了才真正调用 reranker.rerank()（在
try/except 内，异常仍走上面的 error 降级路径，finally 释放信号量）；抢不到
直接跳过重排调用本身（不是"调用了但丢弃结果"，是根本不发起这次重排请求），
降级为融合序——这是保证尾延迟的关键：非阻塞跳过让超额请求立即返回，而不是
排队等前面的请求让出信号量。
rerank_status 是这次查询在重排环节的落点，四态："on"=真正重排成功；
"shed_load"=信号量已满，主动跳过（未调用 reranker）；"error"=抢到了信号量
但 reranker.rerank() 抛异常（H2 既有降级路径）；"off"=Retriever 根本没配置
reranker（reranker=None，等价于既有的 rerank_active=False 场景）。
放进 trace["rerank_status"]（debug=True 时对外可见），同时用
RetrievalResult.rerank_status 冗余暴露一份（非 debug 调用方——如 API 的
/healthz 计数场景——不必解析 trace 字典）。
计数器（rerank_total/rerank_shed_load_total/rerank_error_total）挂在
Retriever 实例上（单进程 uvicorn 部署下等价于"进程级"，见 rerank_stats
property），用一把 threading.Lock 保护自增——多线程同时命中同一个
Retriever 实例是常态（那正是这个降级机制要处理的场景）。

上下文预算（T17）：_assemble 原先是"凑满 top_k 个不同父块即止"，不看这些
父块加起来多长——单块上限（max_parent_chars，D6）只管每块各自不超 4000 字，
10 个块就是 4 万字，长表格类文档很容易把 prompt 顶到模型上限附近，还稀释掉
真正的命中内容。RetrievalStrategy.context_budget（字符）= 一次检索交给生成层
的上下文总量上限，三层可配（全局/按库/按请求）。**None（缺省）时整条预算
分支不生效**，输出与 T17 之前逐字节一致（验收契约）。
给了预算则停止条件变成"预算耗尽 **或** 凑满 top_k"：命中表格块时表格优先
（整表放得下就给整表，放不下退化为表头+命中行+「共 N 行（已截断）」），普通
文本块按剩余预算截窗。被裁剪过的块上挂一个**非字段**属性 truncated_note
（说明行文本），经 Generator.citations 传给前端在引用旁标「已截断」。

标记为什么走非字段属性而不是 ContextBlock 新增字段：ContextBlock 是
dataclass，`asdict()` 会无条件序列化全部字段（包括默认值），加一个
`truncated: bool = False` 就会让 /api/kb/{id}/search 的**每一个** block
多出一个键——预算没开也照加，既有调用方的响应形状被改。普通实例属性不进
asdict、不进 == 比较、不在 dataclass 字段表里，预算为 None 时它压根不存在，
读它的唯一入口是 getattr(block, "truncated_note", None)。
"""
import json
import logging
import math
import threading
from dataclasses import dataclass, replace

from kbase.models import Chunk, Document
from kbase.params import (group_matches_range, is_range_condition,
                          layout_param_bounds, numeric_bounds)
from kbase.plugins.base import Embedder, VectorStore
# T17 表格降级复用分块器自己的表格解析/重建（parse_table / _table_markdown）：
# 解析口径必须与摄取时是**同一个**实现，否则"检索看到的表"与"索引里的表"
# 可能对不上（跨页断表合并等已在摄取侧处理过）。
from kbase.plugins.chunkers.structure import _table_markdown, parse_table

logger = logging.getLogger(__name__)


@dataclass
class ContextBlock:
    doc_id: str
    doc_name: str
    heading_path: str
    text: str          # 父块全文（small-to-big 的"big"）
    snippet: str       # 命中的叶子块原文（引用展示用）
    score: float
    # M5-2 引用定位：命中叶子块在源文件中的页码（文本层 PDF 摄取时回填，
    # 其他格式/老数据为 None）。取叶子而非父块的页——引用要跳去的是命中处。
    page: int | None = None
    # M6-2 多库联合问答：命中块所属知识库（跨库检索时溯源需要区分来源库；
    # 单库检索时也回填，前端可选择性展示）。
    kb_id: str | None = None


@dataclass
class RetrievalResult:
    blocks: list        # list[ContextBlock]
    trace: dict | None = None
    rerank_status: str | None = None   # "on"/"shed_load"/"error"/"off"，见模块顶部注释


def rrf_fuse(ranked_lists: list[list[tuple[str, float]]], k: int = 60
             ) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion。输入各路 (chunk_id, score) 有序列表（已按相关性降序），
    输出按融合分降序的 (chunk_id, fused_score)。分数值本身不参与计算，只用名次——
    这是 RRF 的核心思路，用来把量纲不同、不可比较的多路排名合并为统一序。"""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (cid, _s) in enumerate(ranked, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def chunk_meta_matches(meta_json: str | None, filters: dict,
                       layout_json: str | None = None) -> bool:
    """canonical 过滤语义（与向量库两档适配器对齐）：字段间 AND、列表值 OR、
    值统一转 str 比较；chunk 元数据缺字段或整体为空 → 不匹配。纯函数，单测直击。

    范围条件（{"字段":{"gte":..,"lte":..}}）走 layout_json 里的**块级参数区间**
    ——文档级 meta 是整份文档共享的 front matter，放不下行组级的数值区间。
    判定复用 kbase/params.py 的 group_matches_range，与两个向量库适配器同源。
    """
    range_conds = {k: v for k, v in filters.items() if is_range_condition(v)}
    plain = {k: v for k, v in filters.items() if k not in range_conds}

    for k, cond in range_conds.items():
        bounds = numeric_bounds(cond)
        if bounds is None:
            continue
        lo, hi = bounds
        pmin, pmax = layout_param_bounds(layout_json, k)
        if not group_matches_range(pmin, pmax, lo, hi):
            return False

    if not plain:
        return True
    if not meta_json:
        return False
    try:
        meta = json.loads(meta_json)
    except (ValueError, TypeError):
        return False
    for k, wanted in plain.items():
        wanted_vals = wanted if isinstance(wanted, list) else [wanted]
        stored = meta.get(k)
        if stored is None:
            return False
        stored_vals = stored if isinstance(stored, list) else [stored]
        stored_set = {str(x) for x in stored_vals}
        if not any(str(w) in stored_set for w in wanted_vals):
            return False
    return True


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


_ELLIPSIS = "…"


def _window_parent_text(parent_text: str, leaf_text: str, max_chars: int) -> str:
    """D6：父块全文超过 max_chars 时，以命中叶子文本在父块中首次出现的位置
    为中心截一个不超过 max_chars 的窗口，保证返回内容里一定含有命中叶子——
    否则命中叶子靠后时简单头部截断会把它切掉，答案就丢了关键上下文。
    截断处加 … 标记（开头/结尾视是否真的被截去而定）。
    找不到叶子文本（理论上不应发生，防御性兜底）时退化为头部截断。"""
    if len(parent_text) <= max_chars:
        return parent_text
    idx = parent_text.find(leaf_text)
    if idx == -1:
        return parent_text[:max_chars] + _ELLIPSIS
    center = idx + len(leaf_text) // 2
    half = max_chars // 2
    start = max(0, center - half)
    end = min(len(parent_text), start + max_chars)
    start = max(0, end - max_chars)     # 尾部不够时把窗口往前挪，窗口宽度尽量吃满
    windowed = parent_text[start:end]
    if start > 0:
        windowed = _ELLIPSIS + windowed
    if end < len(parent_text):
        windowed = windowed + _ELLIPSIS
    return windowed


def _window_within(text: str, leaf_text: str, max_chars: int) -> str:
    """T17 预算版截窗：与 _window_parent_text 同思路（以命中叶子为中心，两端
    被截处加 …），但**保证结果长度 <= max_chars**。

    为什么不直接复用 _window_parent_text：它按"上限 + 少量 … 余量"设计（D6
    那边是单块上限，超一两个字符无所谓），预算却是硬上限。它还有第二个偏差：
    窗口两端都被截时会加两个 …，于是 max_chars 较小时实际返回可能超过上限
    若干字符（甚至少切一大截内容）。这里把「窗口 + 至多两个 …」当成整体去
    拟合预算：先按预算减掉两个 … 的占位，再取窗口；正好顶着预算就只在必要
    的那一端加 …。

    找不到叶子文本（理论上不应发生）时退化为头部截断——预算之下宁可按头截，
    也不返回超预算的全文。max_chars 太小以致一个字符都放不下（<1）时返回空串，
    由调用方跳过这一块。"""
    if len(text) <= max_chars:
        return text
    payload = max_chars - 2 * len(_ELLIPSIS)
    if payload < 1:
        return ""
    idx = text.find(leaf_text)
    if idx == -1:
        return text[:max_chars]
    center = idx + len(leaf_text) // 2
    start = max(0, center - payload // 2)
    end = min(len(text), start + payload)
    start = max(0, end - payload)       # 尾部不够时把窗口往前挪，窗口尽量吃满
    lead = _ELLIPSIS if start > 0 else ""
    tail = _ELLIPSIS if end < len(text) else ""
    room = max_chars - len(lead) - len(tail)
    windowed = text[start:end][:room] if room >= 1 else ""
    if not windowed:
        return ""
    return f"{lead}{windowed}{tail}"


# T17 表格降级后的说明行格式（也是前端「已截断」标的数据来源）。
_TRUNCATED_NOTE = "共 {n} 行（已截断）"
# 普通文本块按预算截窗的说明（与表格的区分开：文本截的是"窗口"，不是行数）。
_TEXT_TRUNCATED_NOTE = "本段已按上下文预算截断（已截断）"


def _table_layout_kind(layout_json: str | None) -> str | None:
    """块级版式元数据里的 kind（摄取时写：表格块为 "table"，见
    StructureChunker）。缺失/解析失败返回 None——按普通文本块处理，
    版式元数据永远不该成为检索能否工作的前提。"""
    if not layout_json:
        return None
    try:
        kind = json.loads(layout_json).get("kind")
    except (ValueError, TypeError, AttributeError):
        return None
    return kind if isinstance(kind, str) else None


def _truncated_table(leaf_text: str, budget: int) -> tuple[str, str] | None:
    """T17 表格降级：整表放不进预算时，退化为「表头 + 命中行 + 共 N 行（已截断）」。

    为什么不直接按字符砍：Markdown 表被从中间砍断后，剩下的行会脱离表头变成
    "| 350 | 500 |" 这类裸值组，模型据此作答等于让它猜列义——比少给几行更危险
    （M6 表格感知要消灭的正是这种裸行组）。退化版保留**表头**（列名↔值的绑定）
    与**命中行**（叶子块本身就是检索命中的那组数据行），并把总行数写进说明行，
    让模型知道表被截过，而不是"表里没有这一项"。

    返回 (重建文本, 说明行)；放不下（连"表头+一行"都塞不进预算）、或叶子文本
    不是合法表格 → None，调用方按别的路径处理，绝不因解析失败丢内容。"""
    parsed = parse_table(leaf_text)
    if parsed is None:
        return None
    header, rows = parsed
    if not rows:
        return None
    note = _TRUNCATED_NOTE.format(n=len(rows))
    kept: list[list[str]] = []
    for row in rows:
        candidate = _table_markdown(header, [*kept, row])
        if len(candidate) + 1 + len(note) > budget:
            break
        kept.append(row)
    if not kept:
        # 连"表头+一行"都放不进预算：降级版本身就没意义（模型会拿着一张没有
        # 数据行的表作答，比不给更糟），交给调用方跳过这一块。
        return None
    return f"{_table_markdown(header, kept)}\n{note}", note


class Retriever:
    def __init__(self, session_factory, embedder: Embedder, store: VectorStore,
                 keyword_index=None, reranker=None,
                 candidates: int = 20, rrf_k: int = 60,
                 max_parent_chars: int = 4000, max_concurrency: int = 8,
                 embedder_resolver=None):
        self._sf = session_factory
        self._embedder = embedder
        self._store = store
        # M5-2 KB 级向量模型：查询向量必须与该库摄取时用的是同一个模型，
        # resolver(kb_id) 保证这一点；未提供时退回单一 embedder（既有行为）。
        self._embedder_resolver = embedder_resolver
        self._kw = keyword_index
        self._reranker = reranker
        self._candidates = candidates
        self._rrf_k = rrf_k
        self._max_parent_chars = max_parent_chars
        # 有界并发（M4-2 H6.5）：一次性创建，Retriever 实例被所有请求共享，
        # 每个 retrieve() 跑在各自的线程池线程里，用 threading（非 asyncio）
        # 的信号量，见模块顶部"重排过载自适应降级"注释。
        self._rerank_sem = threading.BoundedSemaphore(max(1, max_concurrency))
        self._stats_lock = threading.Lock()
        self._rerank_total = 0
        self._rerank_shed_load_total = 0
        self._rerank_error_total = 0

    @property
    def rerank_active(self) -> bool:
        return self._reranker is not None

    @property
    def rerank_stats(self) -> dict:
        """进程级（单 uvicorn 进程部署下等价）重排计数器快照，供 /healthz 暴露。"""
        with self._stats_lock:
            return {
                "rerank_total": self._rerank_total,
                "rerank_shed_load_total": self._rerank_shed_load_total,
                "rerank_error_total": self._rerank_error_total,
            }

    def _record(self, status: str) -> None:
        with self._stats_lock:
            self._rerank_total += 1
            if status == "shed_load":
                self._rerank_shed_load_total += 1
            elif status == "error":
                self._rerank_error_total += 1

    def retrieve(self, kb_id: str, query: str, top_k: int = 5, debug: bool = False,
                 strategy=None, filters: dict | None = None):
        """debug=False 返回 list[ContextBlock]（向后兼容）；
        debug=True 返回 RetrievalResult(blocks, trace)。

        strategy（M6-1.5，RetrievalStrategy|None）：KB 级/请求级检索策略。
        None=沿用构造参数（既有行为字节级不变）。策略只能**关闭**已安装的
        能力（keyword_index/reranker 实例仍是最终门），开不出部署里没有的路。

        filters（ztenith 流水线/方案卡）：{字段: 值|[值,...]}，字段间 AND、
        列表内 OR，作用于 chunk 元数据（front matter 摄取时落库）。稠密路
        由向量库原生过滤（两档适配器语义已对齐）；关键词路 BM25 索引没有
        元数据概念，检索后按 Chunk.meta 后过滤——两路进融合的候选集口径
        一致，融合排序逻辑不动。

        T17 上下文预算从 strategy.context_budget 取（三层合并在
        resolve_strategy 完成，这里不再另立请求级参数）：None=不限，组装层
        整条预算分支不生效，返回形状与 T17 之前逐字节一致；非 None 时被裁剪
        过的块会在 trace["truncated"] 里留一条说明（debug=True 可见），
        会话链路的「已截断」标走 Generator.citations 的旁路载荷。
        """
        trace: dict = {}
        use_keyword = strategy.use_keyword if strategy is not None else True
        use_rerank = strategy.use_rerank if strategy is not None else True
        candidates = (strategy.candidates if strategy is not None
                      else self._candidates)
        embedder = (self._embedder_resolver(kb_id)
                    if self._embedder_resolver else self._embedder)
        vec = embedder.embed([query])[0]
        dense_hits = self._store.search(kb_id, vec, top_k=candidates,
                                        filters=filters or None)
        dense = [(h.chunk_id, h.score) for h in dense_hits]
        cosine = {h.chunk_id: h.score for h in dense_hits}
        trace["dense"] = dense

        if self._kw is not None and use_keyword:
            kw_hits = self._kw.search(kb_id, query, top_k=candidates)
            if filters:
                kw_hits = self._filter_hits_by_meta(kw_hits, filters)
            keyword = [(h.chunk_id, h.score) for h in kw_hits]
            trace["keyword"] = keyword
            fused = rrf_fuse([dense, keyword], k=self._rrf_k)[: candidates]
        else:
            fused = dense[: candidates]
        trace["fused"] = fused

        candidate_ids = [cid for cid, _ in fused]
        # 关键词路独有候选补算余弦（用 Chroma 存量向量，保证阈值语义统一）
        missing = [cid for cid in candidate_ids if cid not in cosine]
        if missing:
            cosine.update(self._cosine_from_store(kb_id, missing, vec))

        ordered = None
        if self._reranker is None or not use_rerank:
            rerank_status = "off"
        else:
            acquired = self._rerank_sem.acquire(blocking=False)
            if not acquired:
                # 有界并发已打满：不发起这次重排调用（不是"调用了但丢弃结果"），
                # 直接降级为融合序——非阻塞跳过是保证尾延迟的关键，见模块顶部
                # "重排过载自适应降级"注释。
                rerank_status = "shed_load"
                logger.debug("重排并发已满（max_concurrency 已用尽），"
                             "本次查询降级为融合排序")
            else:
                try:
                    texts = self._leaf_texts(candidate_ids)
                    scores = self._reranker.rerank(
                        query, [texts.get(cid, "") for cid in candidate_ids])
                    reranked = sorted(zip(candidate_ids, scores),
                                      key=lambda kv: kv[1], reverse=True)
                    trace["reranked"] = reranked
                    ordered = list(reranked)
                    rerank_status = "on"
                except Exception as e:  # noqa: BLE001 —— TEI 服务瞬时不可达/超时等任意异常都应降级，而不是让某一类异常穿透成 500
                    logger.warning("重排失败，本次查询降级为融合排序: %s", e)
                    rerank_status = "error"
                finally:
                    self._rerank_sem.release()
            self._record(rerank_status)
        trace["rerank_status"] = rerank_status
        if ordered is None:
            ordered = [(cid, cosine.get(cid, 0.0)) for cid in candidate_ids]

        # top_k 语义 = 去重后的父块数：全量候选按序喂给组装层，凑满 top_k 个
        # 不同父块即止。若在叶子层截断，单文档多叶子霸榜时去重会把结果收缩到
        # 少于 top_k 块，挤掉排位靠后的其他来源。
        # T17：strategy 带了上下文预算时，停止条件追加"预算耗尽"（见 _assemble）；
        # 预算为 None（缺省）时这次调用的参数与 T17 之前逐字节相同。
        context_budget = (getattr(strategy, "context_budget", None)
                          if strategy is not None else None)
        blocks = self._assemble(ordered, top_k, context_budget)
        # 截断说明只进 trace（debug=True 可见），不进 ContextBlock 的字段——
        # 保持既有响应形状分毫不动，见模块顶部注释。
        if debug:
            truncated = {str(i + 1): b.truncated_note
                         for i, b in enumerate(blocks)
                         if getattr(b, "truncated_note", None)}
            if truncated:
                trace["truncated"] = truncated
            return RetrievalResult(blocks=blocks, trace=trace, rerank_status=rerank_status)
        return blocks

    def _filter_hits_by_meta(self, hits, filters: dict):
        """关键词路元数据后过滤：批量取 Chunk.meta（JSON）判 canonical 语义
        （chunk_meta_matches 纯函数）。meta 为 NULL 的 chunk（非方案卡文档）
        不匹配任何过滤条件——带过滤的检索意图就是"只要有该元数据的内容"。"""
        if not hits:
            return hits
        with self._sf() as s:
            rows = s.query(Chunk.id, Chunk.meta, Chunk.layout).filter(
                Chunk.id.in_([h.chunk_id for h in hits])).all()
        metas = {cid: (m, lay) for cid, m, lay in rows}
        return [h for h in hits
                if chunk_meta_matches(
                    metas.get(h.chunk_id, (None, None))[0], filters,
                    metas.get(h.chunk_id, (None, None))[1])]

    def _cosine_from_store(self, kb_id: str, ids: list[str], query_vec: list[float]
                            ) -> dict[str, float]:
        vectors = self._store.get_vectors(kb_id, ids)
        return {cid: _cosine(query_vec, v) for cid, v in vectors.items()}

    def _leaf_texts(self, ids: list[str]) -> dict[str, str]:
        """批量取叶子块文本（heading_path+"\n"+text，与向量化时一致），供重排使用。"""
        if not ids:
            return {}
        with self._sf() as s:
            leaves = s.query(Chunk).filter(Chunk.id.in_(ids)).all()
            return {c.id: f"{c.heading_path}\n{c.text}" for c in leaves}

    def _assemble(self, ordered: list[tuple[str, float]],
                  top_k: int, context_budget: int | None = None
                  ) -> list[ContextBlock]:
        """叶子命中 -> 父块上下文组装（small-to-big，M1 既有逻辑）。
        按 ordered 顺序遍历，同一父块下的多个叶子命中去重，只返回一次；
        score 取 ordered 中该叶子对应的分数（融合/重排/余弦，视管道档位而定）。
        凑满 top_k 个不同父块即停（top_k 语义 = 父块数，见 retrieve 注释）。
        父块全文超过 max_parent_chars 时按命中叶子的位置截窗（D6，见
        _window_parent_text），避免超长父块把 prompt 撑爆或稀释掉真正相关
        的叶子内容。

        返回 blocks；被预算裁剪过的块上多挂一个**非字段**属性
        `truncated_note`（说明行文本），预算为 None 时该属性根本不存在。

        标记为什么走"非字段属性"而不是 ContextBlock 新增字段：ContextBlock 是
        dataclass，`asdict()` 会无条件序列化**全部**字段（含默认值），加一个
        `truncated: bool = False` 就会让 /api/kb/{id}/search 的每一个 block
        多出一个键——预算没开也照加。而普通实例属性不进 `asdict`、不进
        `==` 比较、不进 dataclass 字段表：预算为 None 时读它的 read 侧
        （Generator.citations 的 getattr）拿到 False，序列化形状分毫不动。
        这正是上一次尝试踩中的坑（见模块顶部注释）。

        T17（context_budget 非 None 时）加了什么：
        - 停止条件从"凑满 top_k"变成"**预算耗尽 或** 凑满 top_k"（任一先到即停）；
        - 预算是**硬上限**：预算按 text 字符数累计（= 真正进 prompt 的那段，
          标题/snippet 只是展示字段，不占模型的上下文预算）；
        - 表格块优先：整表放得进剩余预算就给整表；放不进才退化为「表头 +
          命中行 + 共 N 行（已截断）」，并记一条说明（前端引用旁标「已截断」）；
        - 普通文本块超预算时按剩余预算截窗（复用 D6 的 _window_parent_text，
          保证窗口仍以命中叶子为中心，答案不会因为截断而丢命中句）；
        - 放不下的块（截窗后仍无内容、或表格连表头都塞不进）**不进入**结果：
          宁可少给一块，也不给一块只有占位符的空上下文。

        context_budget=None 时上面整段分支都不执行，下面的循环体与 T17 之前
        逐字节同参（验收契约，见 tests/test_retriever.py 的 None 预算用例）。"""
        budgeted = context_budget is not None
        consumed = 0
        blocks: list[ContextBlock] = []
        seen_parents: set[str] = set()
        with self._sf() as s:
            for chunk_id, score in ordered:
                if len(blocks) >= top_k:
                    break
                if budgeted and consumed >= context_budget:
                    break             # 预算耗尽（停止条件之一，另一个是 top_k）
                leaf = s.get(Chunk, chunk_id)
                if leaf is None:
                    continue
                # M6-1 停用块防御兜底：停用=索引成员摘除（chunk_admin），正常
                # 情况下走不到这里；索引清理万一失手也不把停用块漏给生成层。
                if leaf.enabled is False:
                    continue
                parent = s.get(Chunk, leaf.parent_id) if leaf.parent_id else leaf
                if parent.id in seen_parents:
                    continue
                doc = s.get(Document, leaf.doc_id)
                text = _window_parent_text(parent.text, leaf.text, self._max_parent_chars)
                note = None
                if budgeted:
                    text, note = self._fit_to_budget(
                        text, leaf, context_budget - consumed)
                    if text is None:
                        # 放不下就跳过（不占 seen_parents：同一父块下更短的
                        # 叶子之后仍有机会以完整窗口进来）。
                        continue
                seen_parents.add(parent.id)
                if budgeted:
                    consumed += len(text)
                block = ContextBlock(
                    doc_id=leaf.doc_id,
                    doc_name=doc.filename if doc else "未知文档",
                    heading_path=parent.heading_path,
                    text=text,
                    snippet=leaf.text,
                    score=score,
                    page=leaf.page,
                    kb_id=leaf.kb_id,
                )
                if note is not None:
                    block.truncated_note = note      # 非字段属性，见方法注释
                blocks.append(block)
        return blocks

    def _fit_to_budget(self, text: str, leaf: Chunk, remaining: int
                       ) -> tuple[str | None, str | None]:
        """T17：把一块文本压进剩余预算，返回 (文本, 截断说明)。文本放不下且
        无法优雅降级时返回 (None, None) 表示"这一块不要了"。

        预算为硬上限（不像 D6 的 max_parent_chars 允许"上限 + … 余量"），
        所以预算内的文本原样返回——常态是"放得下"，那条路径一次字符串操作
        都不做。"""
        if remaining <= 0:
            return None, None
        if len(text) <= remaining:
            return text, None
        if _table_layout_kind(leaf.layout) == "table":
            # 表格优先：整表放不下才降级（表的价值在完整性，且截断后的裸行组
            # 会让模型猜列义，见 _truncated_table）。
            degraded = _truncated_table(leaf.text, remaining)
            if degraded is not None:
                return degraded
            return None, None         # 降级版也放不下：跳过，不留空表壳
        # 普通文本：按剩余预算截窗，窗口仍以命中叶子为中心（D6 同源规则）。
        windowed = _window_within(text, leaf.text, remaining)
        if not windowed:
            return None, None
        return windowed, _TEXT_TRUNCATED_NOTE

    def _union_weight(self, kb_id: str) -> float:
        """多库联查的库级权重（对标#8，阿里云百炼"按库配权重"）：读
        KB.config 的 union_weight（0.1~10，缺省 1.0=既有行为）。配置损坏
        按 1.0 处理——权重是排序微调，不值得让联查整体失败。"""
        import json as _json

        from kbase.models import KnowledgeBase
        with self._sf() as s:
            kb = s.get(KnowledgeBase, kb_id)
        try:
            w = float(_json.loads(kb.config).get("union_weight", 1.0)) \
                if kb is not None and kb.config else 1.0
        except (ValueError, TypeError, AttributeError):
            return 1.0
        return min(max(w, 0.1), 10.0)

    def retrieve_multi(self, kb_ids: list[str], query: str, top_k: int = 5,
                       strategy=None, filters: dict | None = None):
        """M6-2 跨库联合检索（散射-聚合）：对每个库独立跑一次 retrieve()
        （复用其全套策略/向量模型/重排逻辑），把各库结果块合并后按分数全局
        重排，取前 top_k。

        为什么全局按分数合并可行：重排分（交叉编码器 query-doc 相关度）跨库
        天然可比；未重排时的余弦分在各库用同一 embedder 时也可比（不同
        embedder 的库混排是近似——但那是 KB 级向量模型的少见组合，v1 接受）。
        每库先取 top_k 个候选块再合并，保证任一库的强命中不会被别库淹没。

        库级权重（对标#8）：各库分数乘以其 union_weight 后再全局排序——
        运营侧把权威库调高/杂讯库调低的旋钮；默认全 1.0，行为与 M6-2 不变。

        **多库预算规则（T17）：预算是按库平分的，不是各库共享一整份。**
        规则：单库份额 = strategy.context_budget // len(kb_ids)（向下取整，
        余数不进任何库——预算因此是**严格不大于**配置值的硬上限）。
        为什么平分而不是共享：库枚举顺序不该决定谁拿到上下文。共享一份时，
        排在 kb_ids 前面的库先把预算吃光，后面的库无论多相关都拿不到东西；
        这等于让"调用方传参顺序"变成一个隐式的、无人知晓的排序旋钮，而
        retrieve_multi 的合并语义是**全局按分数**重排（散射-聚合）——按库
        平分是唯一与"库之间对等、只由分数分胜负"这条既有契约一致的分法。
        代价：命中集中在某一库时，别的库的份额用不上，总量可能远低于配置值
        ——这是**有意**偏保守（少给上下文，不会给错上下文），且各库词法上
        都能拿到自己最相关的块；要"按需再分配"就得引入两轮检索（先探每库
        实际需要多少），那是另一张卡的取舍。
        单库调用（retrieve）不受影响：那一份全额给该库。
        strategy.context_budget 为 None 时下面一切照旧（M6-2 行为不变）。"""
        kb_budget = (getattr(strategy, "context_budget", None)
                     if strategy is not None else None)
        if kb_budget is not None and kb_ids:
            kb_budget = max(1, kb_budget // len(kb_ids))
        merged: list[ContextBlock] = []
        for kb_id in kb_ids:
            weight = self._union_weight(kb_id)
            # 逐库把份额塞进策略副本（frozen dataclass 用 dataclasses.replace
            # 派生，不原地改调用方传进来的对象——请求级策略是共享的）。
            sub = (replace(strategy, context_budget=kb_budget)
                   if kb_budget is not None else strategy)
            for block in self.retrieve(kb_id, query, top_k, strategy=sub,
                                       filters=filters):
                if weight != 1.0:
                    block.score = block.score * weight
                merged.append(block)
        merged.sort(key=lambda b: b.score, reverse=True)
        # 各库份额之和 <= 总预算（除法向下取整），合并后已经天然在预算内，
        # 无需再截；这里仍只按 top_k 取。
        return merged[:top_k]
