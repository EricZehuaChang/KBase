import pytest


def test_module_importable_without_model():
    """import 本身不触发模型下载（sentence_transformers 延迟到实例化）。"""
    import kbase.plugins.embedders.bge_local  # noqa: F401


@pytest.mark.external
def test_bge_local_embed_shape():
    from kbase.plugins.embedders.bge_local import BgeLocalEmbedder
    e = BgeLocalEmbedder(model="BAAI/bge-m3")
    vecs = e.embed(["住房补贴的申领条件", "经费保障"])
    assert len(vecs) == 2
    assert len(vecs[0]) == e.dimension == 1024
