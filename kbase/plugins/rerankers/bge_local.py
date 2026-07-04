from kbase.plugins.registry import registry


@registry.register("reranker", "bge-local")
class BgeLocalReranker:
    def __init__(self, model: str = "BAAI/bge-reranker-v2-m3", device: str | None = None):
        from sentence_transformers import CrossEncoder   # 延迟 import

        self._model = CrossEncoder(model, device=device)

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        return [float(s) for s in
                self._model.predict([(query, t) for t in texts])]
