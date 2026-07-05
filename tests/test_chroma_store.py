"""VectorStore 契约测试：Chroma 与 Qdrant 两个适配器共跑同一组断言。

参数化 fixture `store` 按 backend 名（"chroma" / "qdrant-memory"）构造对应
实现——Qdrant 用 `QdrantClient(":memory:")` 本地模式，无需起容器，构造成本
与 Chroma 的临时目录持久化相当，可直接进常规单测。两个适配器对外暴露的
score 语义均为余弦相似度 [-1, 1]（ChromaStore 用 1-cosine_distance 换算，
QdrantStore 的 Distance.COSINE 集合直接返回相似度，已在实现里验证过），
因此同一组断言可以字面复用，不需要区分 backend 做特殊断言。
"""
import pytest

from kbase.plugins.vectorstores.chroma_store import ChromaStore
from kbase.plugins.vectorstores.qdrant_store import QdrantStore


@pytest.fixture(params=["chroma", "qdrant-memory"])
def store(request, tmp_path):
    if request.param == "chroma":
        return ChromaStore(persist_dir=str(tmp_path / "chroma"))
    return QdrantStore(location=":memory:")


def _mk(store, fake_embedder):
    vecs = fake_embedder.embed(["甲", "乙", "丙"])
    store.upsert("kb1",
                 ids=["c1", "c2", "c3"],
                 vectors=vecs,
                 metas=[{"doc_id": "d1"}, {"doc_id": "d1"}, {"doc_id": "d2"}])
    return vecs


def test_search_returns_hits(store, fake_embedder):
    vecs = _mk(store, fake_embedder)
    hits = store.search("kb1", vecs[0], top_k=2)
    assert hits[0].chunk_id == "c1"          # 自身向量最相近
    assert hits[0].score >= hits[1].score


def test_filter_by_doc(store, fake_embedder):
    vecs = _mk(store, fake_embedder)
    hits = store.search("kb1", vecs[0], top_k=3, filters={"doc_id": "d2"})
    assert {h.chunk_id for h in hits} == {"c3"}


def test_upsert_empty_batch_is_noop(store):
    store.upsert("kb1", ids=[], vectors=[], metas=[])   # 不应抛异常
    assert store.search("kb1", [0.0] * 8, top_k=3) == []


def test_delete_by_doc(store, fake_embedder):
    vecs = _mk(store, fake_embedder)
    store.delete("kb1", doc_id="d1")
    hits = store.search("kb1", vecs[0], top_k=3)
    assert {h.chunk_id for h in hits} == {"c3"}


def test_delete_collection_removes_all_and_search_returns_empty(store, fake_embedder):
    vecs = _mk(store, fake_embedder)
    store.delete_collection("kb1")
    assert store.search("kb1", vecs[0], top_k=3) == []


def test_delete_collection_tolerates_missing(store):
    store.delete_collection("never-existed")   # 不应抛异常


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb)


def test_get_vectors_roundtrip(store, fake_embedder):
    """get_vectors 只读回存量向量，供检索器补算余弦——真正约束是"方向与
    存入时一致"，而不是逐分量字节相等。Qdrant 的 cosine 距离集合在内部把
    向量归一化存储（已用脚本核实：返回值与原始向量同方向、模长为 1），
    ChromaStore 则原样存回；两者都满足"与原始向量夹角为 0"这个契约，用
    cosine(got, orig) ≈ 1 断言，两个 backend 可复用同一组断言。"""
    vecs = _mk(store, fake_embedder)
    got = store.get_vectors("kb1", ["c1", "c3", "missing"])
    assert set(got) == {"c1", "c3"}
    for cid, orig_vec in [("c1", vecs[0]), ("c3", vecs[2])]:
        assert len(got[cid]) == len(orig_vec)
        assert abs(_cosine(got[cid], orig_vec) - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# QdrantStore 特有：非 UUID 字符串 id 透明转换、endpoint/location 构造分支、
# create_app 的 qdrant 配置分支（缺 endpoint 报错）
# ---------------------------------------------------------------------------

def test_qdrant_store_non_uuid_string_ids_transparent():
    """Qdrant 要求 point id 是 UUID 或整数，拒绝任意字符串（已用脚本核实：
    upsert 字面量 "c1" 直接抛 ValueError）。QdrantStore 应对调用方透明地
    做 str -> UUID5 映射，调用方传入/取回的都还是原始字符串 id。"""
    store = QdrantStore(location=":memory:")
    store.upsert("kb1", ids=["c1", "c2"],
                 vectors=[[1.0, 0.0], [0.0, 1.0]],
                 metas=[{"doc_id": "d1"}, {"doc_id": "d1"}])
    hits = store.search("kb1", [1.0, 0.0], top_k=2)
    # 断言本身就证明了透明转换：Qdrant 内部用 UUID5 存这两个点，若转换/还原
    # 出错，返回的 chunk_id 会是 UUID5 字符串而不是原始的 "c1"/"c2"。
    assert {h.chunk_id for h in hits} == {"c1", "c2"}


def test_qdrant_store_endpoint_takes_priority_over_location(monkeypatch):
    """endpoint 给了就应该走远程 HTTP 客户端分支，即便同时传了 location——
    QdrantClient(url=...) 构造时会尝试探测服务端版本（真实连一次网络，
    连不上时较慢且不稳定），这里 monkeypatch QdrantClient 本身，只断言
    QdrantStore 传给它的是 url= 分支而不是 location= 分支，不发起真实连接。"""
    calls = {}

    class FakeQdrantClient:
        def __init__(self, *args, **kwargs):
            calls.update(kwargs)
            calls["args"] = args

    import kbase.plugins.vectorstores.qdrant_store as mod
    monkeypatch.setattr(mod, "QdrantClient", FakeQdrantClient)

    mod.QdrantStore(endpoint="http://qdrant:6333", location=":memory:")
    assert calls.get("url") == "http://qdrant:6333"
    assert "location" not in calls


def test_create_app_vectorstore_qdrant_missing_endpoint_raises(tmp_path):
    from kbase.api.main import create_app

    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(
        f"""
data_dir: {str(tmp_path / "data").replace(chr(92), "/")}
vectorstore:
  name: qdrant
llm:
  active: fake
  providers:
    - {{name: fake, base_url: 'http://x', api_key_env: FAKE_KEY, model: m}}
""",
        encoding="utf-8")

    class FakeEmbedder:
        dimension = 8

        def embed(self, texts):
            return [[0.0] * 8 for _ in texts]

    with pytest.raises(ValueError, match="vectorstore.endpoint"):
        create_app(config_path=cfg, embedder=FakeEmbedder(), reranker=False, auth="off")
