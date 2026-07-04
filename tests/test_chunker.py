import pytest

from kbase.plugins.chunkers.structure import StructureChunker

DOC = """# 某某政策
## 第一章 总则
第一条 为了推进工作，制定本办法。
第二条 本办法适用于全体单位。
## 第二章 保障措施
第三条 各单位应当保障经费。
前款所述经费由财政承担。
"""


def test_parent_and_leaf_chunks():
    chunks = StructureChunker(chunk_size=500, chunk_overlap=0).chunk(DOC, "test.md")
    parents = [c for c in chunks if c.parent_id is None]
    leaves = [c for c in chunks if c.parent_id is not None]
    assert len(parents) == 2                      # 两章各一个父块
    assert all(l.parent_id in {p.id for p in parents} for l in leaves)


def test_heading_path():
    chunks = StructureChunker(chunk_size=500, chunk_overlap=0).chunk(DOC, "test.md")
    leaf = next(c for c in chunks if c.parent_id and "经费" in c.text)
    assert leaf.heading_path == "test.md > 某某政策 > 第二章 保障措施"


def test_prev_next_chain_within_parent():
    # 强制小 chunk_size 让章节切成多个叶子块
    chunks = StructureChunker(chunk_size=30, chunk_overlap=0).chunk(DOC, "test.md")
    parents = {c.id: c for c in chunks if c.parent_id is None}
    first_parent = next(p for p in parents.values() if "第一章" in p.heading_path)
    siblings = [c for c in chunks if c.parent_id == first_parent.id]
    assert len(siblings) >= 2
    assert siblings[0].prev_id is None
    assert siblings[0].next_id == siblings[1].id
    assert siblings[1].prev_id == siblings[0].id
    assert siblings[-1].next_id is None


def test_parent_holds_full_section_text():
    chunks = StructureChunker(chunk_size=30, chunk_overlap=0).chunk(DOC, "test.md")
    parent = next(c for c in chunks if c.parent_id is None and "第二章" in c.heading_path)
    assert "第三条" in parent.text and "前款所述" in parent.text


def test_overlap_shares_text_between_consecutive_leaves():
    # RecursiveCharacterTextSplitter 的 overlap 以分隔符切出的片段为单位保留：
    # 只有单个片段长度 <= chunk_overlap 时才会作为重叠尾巴带入下一块。
    # DOC 的整行（~16 字符）> overlap=10，会贴 \n 边界切、无重叠，
    # 故此处用短句（以 。 分隔，每句 <= 10 字符）让重叠真实出现。
    doc = ("# 章节\n"
           "甲方负责。乙方配合。丙方监督。丁方审核。"
           "戊方备案。己方存档。庚方复核。辛方归档。\n")
    chunks = StructureChunker(chunk_size=30, chunk_overlap=10).chunk(doc, "test.md")
    siblings = [c for c in chunks if c.parent_id is not None]
    assert len(siblings) >= 2
    # 相邻叶子间应存在重叠文本：后块开头应出现在前块之中
    head = siblings[1].text[:5]
    assert head and head in siblings[0].text


def test_overlap_ge_size_raises():
    with pytest.raises(ValueError, match="chunk_overlap"):
        StructureChunker(chunk_size=10, chunk_overlap=50)


def test_empty_document_returns_empty():
    assert StructureChunker().chunk("", "empty.md") == []


def test_preamble_before_first_heading_kept():
    doc = "文首说明文字。\n# 第一章\n正文。\n"
    chunks = StructureChunker(chunk_size=500, chunk_overlap=0).chunk(doc, "d.md")
    parents = [c for c in chunks if c.parent_id is None]
    pre = next((p for p in parents if "文首说明" in p.text), None)
    assert pre is not None
    assert pre.heading_path == "d.md"     # 无标题层级时路径仅为文档名


def test_public_chunk_params():
    c = StructureChunker(chunk_size=123, chunk_overlap=45)
    assert c.chunk_size == 123 and c.chunk_overlap == 45
