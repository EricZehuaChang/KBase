from kbase.db import make_session_factory
from kbase.index.keyword import KeywordIndex


def _mk(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    idx = KeywordIndex(factory)
    idx.index("kb1", [
        ("c1", "d1", "兵团本级机关事业单位工作人员差旅费管理办法 新兵办发〔2014〕76号"),
        ("c2", "d1", "住房补贴的申领条件为连续工作满两年"),
        ("c3", "d2", "公务卡结算范围包括办公用品采购"),
    ])
    return idx


def test_exact_term_hit(tmp_path):
    idx = _mk(tmp_path)
    hits = idx.search("kb1", "新兵办发〔2014〕76号", top_k=3)
    assert hits and hits[0].chunk_id == "c1"


def test_chinese_word_hit(tmp_path):
    idx = _mk(tmp_path)
    hits = idx.search("kb1", "公务卡的结算范围", top_k=3)
    assert hits and hits[0].chunk_id == "c3"


def test_kb_isolation(tmp_path):
    idx = _mk(tmp_path)
    assert idx.search("kb2", "住房补贴", top_k=3) == []


def test_delete_doc(tmp_path):
    idx = _mk(tmp_path)
    idx.delete_doc("d1")
    assert idx.search("kb1", "住房补贴", top_k=3) == []
    assert idx.search("kb1", "公务卡", top_k=3)      # d2 不受影响


def test_no_match_returns_empty(tmp_path):
    idx = _mk(tmp_path)
    assert idx.search("kb1", "量子力学", top_k=3) == []


def test_query_with_literal_quote_does_not_error(tmp_path):
    """jieba 可能把输入中的字面双引号切成独立 token；FTS5 查询串本身用双引号包裹
    token，若不转义会拼出 \"\"\" 导致 'unterminated string' 语法错误。"""
    idx = _mk(tmp_path)
    assert idx.search("kb1", 'quotes " inside', top_k=3) == []
