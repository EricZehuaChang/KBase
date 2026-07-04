import pytest


class FakeEmbedder:
    """确定性假向量器：不下载模型，向量由文本 hash 生成，维度 8。"""
    dimension = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            h = abs(hash(t))
            out.append([((h >> (i * 4)) % 100) / 100.0 for i in range(8)])
        return out


@pytest.fixture
def fake_embedder():
    return FakeEmbedder()
