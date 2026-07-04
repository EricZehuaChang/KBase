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
