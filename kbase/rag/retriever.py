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
"""
import logging
import math
import threading
from dataclasses import dataclass

from kbase.models import Chunk, Document
from kbase.plugins.base import Embedder, VectorStore

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
                 strategy=None):
        """debug=False 返回 list[ContextBlock]（向后兼容）；
        debug=True 返回 RetrievalResult(blocks, trace)。

        strategy（M6-1.5，RetrievalStrategy|None）：KB 级/请求级检索策略。
        None=沿用构造参数（既有行为字节级不变）。策略只能**关闭**已安装的
        能力（keyword_index/reranker 实例仍是最终门），开不出部署里没有的路。"""
        trace: dict = {}
        use_keyword = strategy.use_keyword if strategy is not None else True
        use_rerank = strategy.use_rerank if strategy is not None else True
        candidates = (strategy.candidates if strategy is not None
                      else self._candidates)
        embedder = (self._embedder_resolver(kb_id)
                    if self._embedder_resolver else self._embedder)
        vec = embedder.embed([query])[0]
        dense_hits = self._store.search(kb_id, vec, top_k=candidates)
        dense = [(h.chunk_id, h.score) for h in dense_hits]
        cosine = {h.chunk_id: h.score for h in dense_hits}
        trace["dense"] = dense

        if self._kw is not None and use_keyword:
            kw_hits = self._kw.search(kb_id, query, top_k=candidates)
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
        blocks = self._assemble(ordered, top_k)
        if debug:
            return RetrievalResult(blocks=blocks, trace=trace, rerank_status=rerank_status)
        return blocks

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
                  top_k: int) -> list[ContextBlock]:
        """叶子命中 -> 父块上下文组装（small-to-big，M1 既有逻辑）。
        按 ordered 顺序遍历，同一父块下的多个叶子命中去重，只返回一次；
        score 取 ordered 中该叶子对应的分数（融合/重排/余弦，视管道档位而定）。
        凑满 top_k 个不同父块即停（top_k 语义 = 父块数，见 retrieve 注释）。
        父块全文超过 max_parent_chars 时按命中叶子的位置截窗（D6，见
        _window_parent_text），避免超长父块把 prompt 撑爆或稀释掉真正相关
        的叶子内容。"""
        blocks: list[ContextBlock] = []
        seen_parents: set[str] = set()
        with self._sf() as s:
            for chunk_id, score in ordered:
                if len(blocks) >= top_k:
                    break
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
                seen_parents.add(parent.id)
                doc = s.get(Document, leaf.doc_id)
                text = _window_parent_text(parent.text, leaf.text, self._max_parent_chars)
                blocks.append(ContextBlock(
                    doc_id=leaf.doc_id,
                    doc_name=doc.filename if doc else "未知文档",
                    heading_path=parent.heading_path,
                    text=text,
                    snippet=leaf.text,
                    score=score,
                    page=leaf.page,
                ))
        return blocks
