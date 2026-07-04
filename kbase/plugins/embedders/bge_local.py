from kbase.plugins.registry import registry


@registry.register("embedder", "bge-local")
class BgeLocalEmbedder:
    def __init__(self, model: str = "BAAI/bge-m3", device: str | None = None):
        # 延迟 import：未装 local-embed extra 时其他插件不受影响
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(model, device=device)
        self.dimension = self._model.get_sentence_embedding_dimension()

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, normalize_embeddings=True,
                                  batch_size=16).tolist()
