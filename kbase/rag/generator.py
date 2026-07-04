"""生成器：prompt 组装 + 流式输出 + 引用编号 + 低相关度拒答。"""
from typing import AsyncIterator

from kbase.rag.retriever import ContextBlock

REFUSAL = "知识库中未找到依据，无法回答该问题。请尝试换个问法，或确认相关文档已导入。"

SYSTEM_PROMPT = (
    "你是一个严谨的知识库问答助手。只依据提供的资料回答问题，"
    "禁止编造资料中不存在的内容。回答中引用资料时标注编号，如[1][2]。"
    "如果资料不足以回答问题，明确说明。使用简体中文回答。"
)

USER_TEMPLATE = """请依据以下资料回答问题。

{sources}

问题：{question}"""


class Generator:
    def __init__(self, llm, min_score: float = 0.3):
        self._llm = llm
        self._min_score = min_score

    def usable_blocks(self, blocks: list[ContextBlock]) -> list[ContextBlock]:
        """按 min_score 过滤出可用于回答/引用的上下文块，保持原有顺序（best-score-first）。

        阈值随检索模式（纯向量 vs 重排后）在构造时传入，不同分数量纲需要不同阈值。
        """
        return [b for b in blocks if b.score >= self._min_score]

    def citations(self, blocks: list[ContextBlock]) -> list[dict]:
        """将 blocks 按顺序编号为引用列表（index 从 1 开始，对应回答中的 [n] 标记）。

        调用方必须传入与 answer_stream() 相同的、经 usable_blocks() 过滤后的列表，
        否则引用编号可能与回答正文中的 [n] 标记不一致。
        """
        return [{"index": i + 1, "doc_id": b.doc_id, "doc_name": b.doc_name,
                 "heading_path": b.heading_path, "snippet": b.snippet,
                 "score": round(b.score, 3)}
                for i, b in enumerate(blocks)]

    def _build_messages(self, question: str, blocks: list[ContextBlock],
                        history: list[dict] | None = None) -> list[dict]:
        sources = "\n\n".join(
            f"[{i + 1}] 出处：{b.heading_path}\n{b.text}"
            for i, b in enumerate(blocks))
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user",
                         "content": USER_TEMPLATE.format(sources=sources, question=question)})
        return messages

    async def answer_stream(self, question: str, blocks: list[ContextBlock],
                            history: list[dict] | None = None) -> AsyncIterator[str]:
        usable = self.usable_blocks(blocks)
        if not usable:
            yield REFUSAL
            return
        # 直接 async for 委托：客户端断开时 GeneratorExit 沿链传播，
        # LLM provider 的信号量随之释放（勿改为手动驱动 __anext__）。
        async for piece in self._llm.stream(self._build_messages(question, usable, history)):
            yield piece
