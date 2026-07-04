import pytest


def test_module_importable_without_model():
    import kbase.plugins.rerankers.bge_local  # noqa: F401


def test_generator_min_score_param():
    """MIN_SCORE 从模块常量改为构造参数，拒答阈值随模式切换。"""
    from kbase.rag.generator import Generator
    from kbase.rag.retriever import ContextBlock

    class FakeLLM:
        async def stream(self, messages, **params):
            yield "答"

    b = ContextBlock(doc_id="d", doc_name="n", heading_path="h",
                     text="t", snippet="s", score=0.32)
    gen_low = Generator(FakeLLM(), min_score=0.3)
    gen_high = Generator(FakeLLM(), min_score=0.35)
    assert gen_low.usable_blocks([b]) == [b]
    assert gen_high.usable_blocks([b]) == []


def test_retriever_rerank_branch_reorders(tmp_path, fake_embedder):
    # 复用 tests/test_hybrid_retriever.py 的 _setup 语料
    from tests.test_hybrid_retriever import _setup

    class FakeReranker:
        def rerank(self, query, texts):
            # 让含"住宿"的文本得高分，其余低分
            return [1.0 if "住宿" in t else 0.1 for t in texts]

    factory, emb, store, kw = _setup(tmp_path, fake_embedder)
    from kbase.rag.retriever import Retriever
    r = Retriever(factory, emb, store, keyword_index=kw, reranker=FakeReranker())
    blocks = r.retrieve("kb1", "火车", top_k=2)
    assert blocks and "住宿" in blocks[0].text          # 重排把住宿块顶到第一
    assert blocks[0].score == 1.0                        # 分数来自 reranker


@pytest.mark.external
def test_bge_reranker_orders_by_relevance():
    from kbase.plugins.rerankers.bge_local import BgeLocalReranker
    r = BgeLocalReranker()
    scores = r.rerank("住房补贴的申领条件",
                      ["申领住房补贴需连续工作满两年", "食堂周五供应红烧肉"])
    assert scores[0] > scores[1]
