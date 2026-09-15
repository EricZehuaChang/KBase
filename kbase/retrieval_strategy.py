"""KB 级检索策略（M6-1.5）：把启动时写死的检索管道拆成三层可配置。

层级（下层只能使用上层已安装的能力，不能凭空开启）：
1. 全局 YAML = **能力开关**：部署装不装关键词索引（retrieval.hybrid）、
   装不装重排模型（retrieval.rerank.enabled）。没装的能力任何层都开不出来
   ——retriever 侧按 keyword_index/reranker 实例是否存在做最终门（None 即无能力）。
2. KB 配置 = **使用开关**：KnowledgeBase.config JSON 的 "retrieval" 键，
   {hybrid/rerank: bool 或缺省, rewrite: off|conditional|always 或缺省,
   candidates: int 或缺省, context_budget: int 或缺省}。缺省=跟随全局默认
   （"通用方式"）。
3. 请求覆盖 = **实验试跑**：检索调试端点可临时强制开/关某一路，用于在
   分析页对比策略效果，不落库。

拒答阈值联动：min_score 的量纲跟着"本次是否重排"走（重排分 vs 余弦分），
per-call 由 pick_min_score 决定——不再用应用启动时的静态值。
"""
import json
from dataclasses import dataclass

from kbase.models import KnowledgeBase

REWRITE_MODES = ("off", "conditional", "always")


@dataclass(frozen=True)
class RetrievalStrategy:
    use_keyword: bool      # 多路召回：是否走关键词路
    use_rerank: bool       # 是否重排（还需 retriever 侧真有 reranker 实例）
    rewrite_mode: str      # 会话查询改写模式
    candidates: int        # 每路召回数/融合候选数
    # T17 上下文预算（字符）：本次检索交给生成层的上下文总量上限。三个来源
    # 同款旋钮——全局 retrieval.context_budget（RetrievalConfig）> KB 配置
    # retrieval.context_budget > 请求覆盖。None（缺省）=不限，组装层退回
    # "凑满 top_k 即止"，检索输出与 T17 之前逐字节一致（验收契约）。
    # 为什么按**字符**而不是 token：字符是摄取/分块层唯一现成的量纲
    # （chunk_size 也是字符），换 token 就要引第三方分词器——本卡硬规则
    # 不允许加依赖。中文下 1 字符≈1 token，中英混排会低估，留余量即可。
    context_budget: int | None = None


def resolve_strategy(cfg, kb_retrieval: dict | None,
                     overrides: dict | None = None) -> RetrievalStrategy:
    """三层合并：请求覆盖 > KB 配置 > 全局默认。None/缺键=继承上一层。"""
    kb = kb_retrieval or {}
    ov = overrides or {}

    def pick(key, kb_key, default):
        if ov.get(key) is not None:
            return ov[key]
        if kb.get(kb_key) is not None:
            return kb[kb_key]
        return default

    return RetrievalStrategy(
        use_keyword=bool(pick("use_keyword", "hybrid", cfg.retrieval.hybrid)),
        use_rerank=bool(pick("use_rerank", "rerank", cfg.retrieval.rerank.enabled)),
        rewrite_mode=str(pick("rewrite_mode", "rewrite", cfg.retrieval.rewrite.mode)),
        candidates=int(pick("candidates", "candidates", cfg.retrieval.candidates)),
        # 预算走 _budget_or_none 归一：KB 配置 JSON 是运营手填的，"0"/""/负数
        # 这类脏值一律当 None（不限）——0 或负数会让组装层一个块都放不进去，
        # 等于静默返回空上下文，那是配置事故而不是意图（与 _union_weight 对
        # 坏配置按 1.0 处理同一个取舍：配置损坏不该让检索整体失败）。
        context_budget=_budget_or_none(
            pick("context_budget", "context_budget", cfg.retrieval.context_budget)),
    )


def _budget_or_none(value) -> int | None:
    """T17：预算归一化。None/非整数/非正数 → None（不限）。"""
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def kb_retrieval_config(sf, kb_id: str) -> dict | None:
    """读 KB 的 retrieval 配置段；库不存在/无配置/解析失败 → None（全局默认）。"""
    with sf() as s:
        kb = s.get(KnowledgeBase, kb_id)
    if kb is None or not kb.config:
        return None
    try:
        return json.loads(kb.config).get("retrieval")
    except (json.JSONDecodeError, TypeError):
        return None


def pick_min_score(cfg, strategy: RetrievalStrategy, rerank_available: bool) -> float:
    """拒答阈值按"本次查询是否真的会重排"选量纲：策略要求重排且部署真有
    reranker → 重排阈值；否则余弦阈值。取代旧的启动期静态 gen_min_score。"""
    if strategy.use_rerank and rerank_available:
        return cfg.retrieval.min_score_rerank
    return cfg.retrieval.min_score_dense
