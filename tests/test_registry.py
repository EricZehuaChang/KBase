import pytest
from kbase.plugins.registry import PluginRegistry


def test_register_and_create():
    reg = PluginRegistry()

    @reg.register("embedder", "fake")
    class Fake:
        def __init__(self, dim: int = 8):
            self.dimension = dim

        def embed(self, texts):
            return [[0.0] * self.dimension for _ in texts]

    inst = reg.create("embedder", "fake", dim=16)
    assert inst.dimension == 16


def test_unknown_plugin_raises():
    reg = PluginRegistry()
    with pytest.raises(KeyError, match="未注册"):
        reg.create("embedder", "nope")
