import hashlib

import pytest


class FakeEmbedder:
    """确定性假向量器：不下载模型，向量由文本 md5 hash 生成（跨进程稳定），维度 8。"""
    dimension = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            h = int(hashlib.md5(t.encode("utf-8")).hexdigest(), 16)
            out.append([((h >> (i * 4)) % 100) / 100.0 for i in range(8)])
        return out


@pytest.fixture
def fake_embedder():
    return FakeEmbedder()
