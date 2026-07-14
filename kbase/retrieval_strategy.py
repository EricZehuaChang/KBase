"""KB 级检索策略（M6-1.5）：把启动时写死的检索管道拆成三层可配置。

层级（下层只能使用上层已安装的能力，不能凭空开启）：
1. 全局 YAML = **能力开关**：部署装不装关键词索引（retrieval.hybrid）、
   装不装重排模型（retrieval.rerank.enabled）。没装的能力任何层都开不出来
   ——retriever 侧按 keyword_index/reranker 实例是否存在做最终门（None 即无能力）。
2. KB 配置 = **使用开关**：KnowledgeBase.config JSON 的 "retrieval" 键，
   {hybrid/rerank: bool 或缺省, rewrite: off|conditional|always 或缺省,
   candidates: int 或缺省}。缺省=跟随全局默认（"通用方式"）。
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
    )


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
