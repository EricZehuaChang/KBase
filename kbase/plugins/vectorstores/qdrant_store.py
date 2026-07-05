"""Qdrant 向量库适配器（standard 部署形态）。

集合惰性创建：Chroma 用 get_or_create_collection 在任意操作时按需建集合；
Qdrant 没有等价的隐式建集合语义（对不存在的集合 search/get/delete 一律
抛 ValueError），这里在 upsert 时惰性创建（维度取首个向量的长度，cosine
距离），其余方法先用 collection_exists 探测，集合不存在按"空结果/no-op"
处理，模拟 Chroma 的隐式创建效果（未摄取过的知识库无集合，此时查询/删除
应视为空，而不是报错）。

分数语义：Qdrant 对 Distance.COSINE 集合的 query_points 直接返回余弦
*相似度*（不是距离，不需要 1-dist 转换——已用脚本验证：同向量 score=1.0，
正交 score=0.0），取值范围与 ChromaStore 的 `1 - cosine_distance` 结果
一致，同为 [-1, 1]（1 完全相同，0 正交，负值反相关）。两个适配器对下游
（阈值判断、trace 展示）暴露的 score 语义完全对齐，可直接互换。

点 ID 类型：Qdrant 要求 point id 是 UUID 或无符号整数，拒绝任意字符串
（如 "c1"）。生产环境的 chunk id 本身就是 str(uuid.uuid4())，天然满足，
但共享契约测试沿用 test_chroma_store.py 的字面量 id（"c1"/"c2"/"c3"），
以及调用方传入的 id 类型不应该是本适配器的约束条件——因此一律做一次
确定性 str -> UUID5 映射（同一原始 id 每次映射到同一个 UUID，命名空间
固定），upsert/search/get_vectors 的返回结果再映射回原始 id 字符串
（原始 id 存进 payload 的 _orig_id 字段，读回来时用它还原，不依赖
UUID5 可逆——单向哈希本身不可逆）。对调用方（Retriever/IngestPipeline
等内核代码）完全透明，看到的仍是自己传入的原始 id 字符串。
"""
import uuid

from qdrant_client import QdrantClient, models

from kbase.plugins.base import Hit
from kbase.plugins.registry import registry

_ID_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _to_point_id(chunk_id: str) -> str:
    """原始 chunk id -> 确定性 UUID5 字符串（同一输入恒定输出，满足 Qdrant
    对 point id 类型的要求）。"""
    return str(uuid.uuid5(_ID_NAMESPACE, chunk_id))


@registry.register("vectorstore", "qdrant")
class QdrantStore:
    def __init__(self, endpoint: str | None = None, api_key: str | None = None,
                 location: str | None = None):
        """endpoint 优先：给了 endpoint 就走远程 HTTP（生产 standard 部署）。
        endpoint 为 None 时用 location（":memory:" 供单测本地模式用，无需
        起容器；也支持磁盘路径的本地持久化，虽然本项目暂不用这条）。"""
        if endpoint is None and location is not None:
            self._client = QdrantClient(location=location)
        else:
            self._client = QdrantClient(url=endpoint, api_key=api_key)

    def _ensure_collection(self, collection: str, dim: int):
        if not self._client.collection_exists(collection):
            self._client.create_collection(
                collection,
                vectors_config=models.VectorParams(
                    size=dim, distance=models.Distance.COSINE))

    def upsert(self, collection, ids, vectors, metas):
        if not ids:
            return
        self._ensure_collection(collection, len(vectors[0]))
        points = []
        for cid, vec, meta in zip(ids, vectors, metas):
            payload = dict(meta or {})
            payload["_orig_id"] = cid
            points.append(models.PointStruct(
                id=_to_point_id(cid), vector=vec, payload=payload))
        self._client.upsert(collection, points=points)

    def search(self, collection, vector, top_k, filters=None):
        if not self._client.collection_exists(collection):
            return []
        query_filter = None
        if filters:
            query_filter = models.Filter(must=[
                models.FieldCondition(key=k, match=models.MatchValue(value=v))
                for k, v in filters.items()
            ])
        res = self._client.query_points(
            collection, query=vector, limit=top_k, query_filter=query_filter)
        hits = []
        for p in res.points:
            payload = dict(p.payload or {})
            orig_id = payload.pop("_orig_id", str(p.id))
            hits.append(Hit(chunk_id=orig_id, score=p.score, meta=payload))
        return hits

    def delete(self, collection, doc_id):
        if not self._client.collection_exists(collection):
            return
        flt = models.Filter(must=[
            models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))])
        self._client.delete(collection, points_selector=models.FilterSelector(filter=flt))

    def delete_collection(self, collection):
        """删除整个集合（知识库级联删除用）。集合不存在时容错，不抛异常——
        knowledge base 从未摄取过任何文档时不会创建集合，此时删除应是
        no-op（与 ChromaStore.delete_collection 语义一致）。"""
        if self._client.collection_exists(collection):
            self._client.delete_collection(collection)

    def get_vectors(self, collection, ids):
        """按 id 取回存量向量（只读）。用于关键词路独有候选的余弦补算，
        保证阈值与纯稠密路语义一致。返回 {id: embedding}，缺失 id 不出现
        在结果中。"""
        if not ids or not self._client.collection_exists(collection):
            return {}
        point_ids = [_to_point_id(cid) for cid in ids]
        records = self._client.retrieve(
            collection, ids=point_ids, with_vectors=True, with_payload=True)
        out = {}
        for r in records:
            orig_id = (r.payload or {}).get("_orig_id", str(r.id))
            out[orig_id] = r.vector
        return out
