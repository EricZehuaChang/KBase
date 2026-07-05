"""PGKeywordIndex 集成测试：真连 PostgreSQL，index→search→delete 全链路。

标记 @pytest.mark.pg：默认被 addopts（-m 'not external and not pg'）跳过，
本地没有 PG 容器时不会跑。设置环境变量 KBASE_TEST_PG_URL（形如
postgresql+psycopg://user:pass@host:5432/db）并显式加 `-m pg` 才会执行——
这是为 H5（真实 GCP standard 栈接入真 PG）预留的验收测试，当前任务的
296+ 全绿套件里这些用例始终 deselected，不依赖本地有 PG。"""
import os
import uuid

import pytest
from sqlalchemy import text

from kbase.db import make_session_factory
from kbase.index.keyword_pg import PGKeywordIndex

_PG_URL = os.environ.get("KBASE_TEST_PG_URL")

pytestmark = pytest.mark.pg


@pytest.fixture
def pg_index():
    if not _PG_URL:
        pytest.skip("KBASE_TEST_PG_URL 未设置，跳过 PG 集成测试")
    factory = make_session_factory(_PG_URL)
    idx = PGKeywordIndex(factory)
    # 每个用例用独立 kb_id 隔离，测试结束清理本用例写入的行
    kb_id = f"kb-{uuid.uuid4().hex[:8]}"
    yield factory, idx, kb_id
    with factory() as s:
        s.execute(text("DELETE FROM chunks_kw WHERE kb_id = :k"), {"k": kb_id})
        s.commit()


def test_index_search_roundtrip(pg_index):
    _factory, idx, kb_id = pg_index
    idx.index(kb_id, [
        ("c1", "d1", "兵团本级机关事业单位工作人员差旅费管理办法"),
        ("c2", "d1", "住房补贴的申领条件为连续工作满两年"),
        ("c3", "d2", "公务卡结算范围包括办公用品采购"),
    ])
    hits = idx.search(kb_id, "公务卡结算范围", top_k=3)
    assert hits and hits[0].chunk_id == "c3"
    assert hits[0].meta == {"route": "keyword"}
    assert hits[0].score > 0


def test_kb_isolation(pg_index):
    _factory, idx, kb_id = pg_index
    idx.index(kb_id, [("c1", "d1", "住房补贴的申领条件")])
    assert idx.search("kb-other-unused", "住房补贴", top_k=3) == []


def test_delete_doc_removes_only_that_doc(pg_index):
    _factory, idx, kb_id = pg_index
    idx.index(kb_id, [
        ("c1", "d1", "住房补贴的申领条件"),
        ("c2", "d2", "公务卡结算范围"),
    ])
    idx.delete_doc("d1")
    assert idx.search(kb_id, "住房补贴", top_k=3) == []
    assert idx.search(kb_id, "公务卡", top_k=3)


def test_delete_kb_removes_all_rows(pg_index):
    _factory, idx, kb_id = pg_index
    idx.index(kb_id, [("c1", "d1", "住房补贴的申领条件")])
    idx.delete_kb(kb_id)
    assert idx.search(kb_id, "住房补贴", top_k=3) == []


def test_reindex_upsert_overwrites_same_chunk_id(pg_index):
    """reindex 场景：同一 chunk_id 重复 index() 应覆盖而不是报唯一约束错误。"""
    _factory, idx, kb_id = pg_index
    idx.index(kb_id, [("c1", "d1", "旧内容占位")])
    idx.index(kb_id, [("c1", "d1", "住房补贴的申领条件")])
    hits = idx.search(kb_id, "住房补贴", top_k=3)
    assert hits and hits[0].chunk_id == "c1"
    assert idx.search(kb_id, "旧内容占位", top_k=3) == []
