import chromadb
from chromadb.config import Settings

from kbase.plugins.base import Hit
from kbase.plugins.registry import registry


@registry.register("vectorstore", "chroma")
class ChromaStore:
    def __init__(self, persist_dir: str = "./data/chroma"):
        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False))

    def _coll(self, collection: str):
        # cosine 距离，与 normalize 后的 bge 向量匹配
        return self._client.get_or_create_collection(
            collection, metadata={"hnsw:space": "cosine"})

    def upsert(self, collection, ids, vectors, metas):
        if not ids:
            return
        self._coll(collection).upsert(ids=ids, embeddings=vectors, metadatas=metas)

    def search(self, collection, vector, top_k, filters=None):
        # score = 1 - cosine_distance = 余弦相似度，取值范围 [-1, 1]（1 完全相同，0 正交，负值反相关）
        res = self._coll(collection).query(
            query_embeddings=[vector], n_results=top_k,
            where=filters or None)
        hits = []
        for cid, dist, meta in zip(res["ids"][0], res["distances"][0],
                                   res["metadatas"][0]):
            hits.append(Hit(chunk_id=cid, score=1 - dist, meta=meta or {}))
        return hits

    def delete(self, collection, doc_id):
        self._coll(collection).delete(where={"doc_id": doc_id})

    def delete_collection(self, collection):
        """删除整个集合（知识库级联删除用）。集合不存在时容错，不抛异常——
        知识库从未摄取过任何文档时不会创建集合，此时删除应是 no-op。"""
        try:
            self._client.delete_collection(collection)
        except Exception:  # noqa: BLE001 —— chromadb 版本间"不存在"异常类型不稳定，统一吞掉
            pass

    def get_vectors(self, collection, ids):
        """按 id 取回存量向量（只读）。用于关键词路独有候选的余弦补算，
        保证阈值与纯稠密路语义一致。返回 {id: embedding}，缺失 id 不出现在结果中。"""
        if not ids:
            return {}
        res = self._coll(collection).get(ids=ids, include=["embeddings"])
        return dict(zip(res["ids"], res["embeddings"]))
