"""共享分词器：FTS5（SQLite）与 PGKeywordIndex（PostgreSQL）两种关键词索引
后端都用同一个 jieba 预分词 + 空格连接策略，保证两种后端召回语义一致。"""
from kbase.index.tokenize import _tokenize


def test_tokenize_splits_chinese_into_space_joined_terms():
    out = _tokenize("公务卡结算范围")
    assert " " in out
    assert "公务卡" in out.split(" ") or "公务" in out.split(" ")


def test_tokenize_strips_empty_terms():
    out = _tokenize("  ")
    assert out == ""


def test_keyword_module_reexports_same_tokenize():
    """既有 FTS5 KeywordIndex 沿用同一实现，而不是各自维护一份副本
    （避免两种后端分词语义漂移）。"""
    from kbase.index import keyword
    from kbase.index.tokenize import _tokenize as shared
    assert keyword._tokenize is shared
