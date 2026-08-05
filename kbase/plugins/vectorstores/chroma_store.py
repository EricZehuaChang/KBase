import chromadb
from chromadb.config import Settings

from kbase.plugins.base import Hit
from kbase.plugins.registry import registry

# 列表元数据的扁平化分隔符：Chroma metadata 只收标量，多值字段（方案卡的
# 数据实体/交互模式等）压成 "a|b|c"。检索过滤时按它切回集合做交集判断。
_LIST_SEP = "|"


def _flatten_meta(meta: dict) -> dict:
    """Chroma metadata 只允许 str/int/float/bool：列表压平为分隔串，None 丢弃。"""
    out = {}
    for k, v in (meta or {}).items():
        if isinstance(v, list):
            out[k] = _LIST_SEP.join(str(x) for x in v)
        elif v is not None:
            out[k] = v
    return out


def _meta_value_matches(stored, wanted: list) -> bool:
    """canonical 过滤语义：字段值相等（或压平列表包含任一目标值）即命中。
    统一转 str 比较——JSON 请求里的 1 与摄取时的 1 可能一边 str 一边 int。"""
    if stored is None:
        return False
    stored_set = (set(str(stored).split(_LIST_SEP))
                  if isinstance(stored, str) else {str(stored)})
    return any(str(w) in stored_set for w in wanted)


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
        self._coll(collection).upsert(
            ids=ids, embeddings=vectors,
            metadatas=[_flatten_meta(m) for m in metas])

    def search(self, collection, vector, top_k, filters=None):
        # score = 1 - cosine_distance = 余弦相似度，取值范围 [-1, 1]（1 完全相同，0 正交，负值反相关）
        #
        # canonical filters（{字段: 值|[值,...]}，字段间 AND、列表内 OR）不走
        # Chroma 原生 where：列表字段已压平成分隔串，原生等值匹配不了"包含"
        # 语义。改为超采后过滤——lite 档（Chroma）数据量小，超采成本可忽略；
        # 代价是极端情况下命中数不足 top_k（如实返回，不补捞第二轮）。
        fetch = top_k if not filters else min(max(top_k * 5, 50), 500)
        res = self._coll(collection).query(
            query_embeddings=[vector], n_results=fetch)
        hits = []
        for cid, dist, meta in zip(res["ids"][0], res["distances"][0],
                                   res["metadatas"][0]):
            meta = meta or {}
            if filters and not all(
                    _meta_value_matches(meta.get(k),
                                        v if isinstance(v, list) else [v])
                    for k, v in filters.items()):
                continue
            hits.append(Hit(chunk_id=cid, score=1 - dist, meta=meta))
            if len(hits) >= top_k:
                break
        return hits

    def delete(self, collection, doc_id):
        self._coll(collection).delete(where={"doc_id": doc_id})

    def delete_ids(self, collection, ids):
        """按 chunk id 精确删除（M6-1 chunk 启停：停用=摘出索引成员）。"""
        if not ids:
            return
        self._coll(collection).delete(ids=ids)

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
