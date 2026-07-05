"""多轮查询改写：把依赖上下文的追问改写为自包含检索问题。
改写只影响检索输入；用户原文的存库与展示不变。失败/超时一律回退原问题。"""
import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_PRONOUNS = ["那", "这", "它", "他", "她", "其", "该", "上述", "前述",
                    "前面", "刚才", "呢", "些", "者"]
_SHORT_LEN = 20

_PROMPT = (
    "以下是一段对话历史：\n{history}\n\n"
    "用户当前的问题是：{question}\n\n"
    "请结合对话历史，把当前问题改写为一个不依赖上下文、语义完整的检索问题。"
    "保留原意，不要扩展无关内容。只输出改写后的问题，不要任何前缀或解释。"
)


def should_rewrite(question: str, history: list[dict], mode: str = "conditional",
                   pronouns: list[str] | None = None) -> bool:
    if mode == "off" or not history:
        return False
    if mode == "always":
        return True
    q = question.strip()
    if len(q) < _SHORT_LEN:
        return True
    if any(p in q for p in (pronouns or DEFAULT_PRONOUNS)):
        return True
    hist_text = " ".join(m["content"] for m in history)
    overlap = sum(1 for ch in set(q) if ch in hist_text)
    return overlap < len(set(q)) * 0.2      # 与历史几乎无字符重叠 → 疑似换话题的省略句


@dataclass
class RewriteResult:
    query: str          # 实际用于检索的问题
    triggered: bool     # 是否命中触发条件
    rewritten: bool     # LLM 改写是否成功生效


class QueryRewriter:
    def __init__(self, llm, mode: str = "conditional",
                 max_wait_s: float = 5.0, pronouns: list[str] | None = None):
        self._llm = llm
        self._mode = mode
        self._max_wait = max_wait_s
        self._pronouns = pronouns

    async def rewrite(self, question: str, history: list[dict]) -> RewriteResult:
        if not should_rewrite(question, history, self._mode, self._pronouns):
            return RewriteResult(query=question, triggered=False, rewritten=False)
        try:
            hist = "\n".join(f"{m['role']}: {m['content']}" for m in history)
            out = await asyncio.wait_for(
                self._llm.complete([{"role": "user", "content":
                                     _PROMPT.format(history=hist, question=question)}]),
                timeout=self._max_wait)
            out = (out or "").strip()
            if out:
                return RewriteResult(query=out, triggered=True, rewritten=True)
        except Exception as e:  # noqa: BLE001 —— 改写永不阻塞主链路
            logger.warning("查询改写失败，回退原问题: %s", e)
        return RewriteResult(query=question, triggered=True, rewritten=False)
