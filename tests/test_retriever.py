import json
from dataclasses import asdict

from kbase.db import make_session_factory
from kbase.ingest.pipeline import IngestPipeline
from kbase.models import Chunk, KnowledgeBase
from kbase.plugins.chunkers.structure import StructureChunker
from kbase.plugins.vectorstores.chroma_store import ChromaStore
from kbase.rag.retriever import Retriever
from kbase.retrieval_strategy import RetrievalStrategy

MD = """# 补贴办法
## 第一章 申领条件
连续工作满两年可申领住房补贴。
补贴对象为在编在岗人员。
## 第二章 标准
每月补贴一千元。
"""


def _setup(tmp_path, fake_embedder):
    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=20, chunk_overlap=0),
                              fake_embedder, store, tmp_path / "files")
    f = tmp_path / "补贴办法.md"
    f.write_text(MD, encoding="utf-8")
    pipeline.ingest_file("kb1", f, "补贴办法.md")
    return Retriever(factory, fake_embedder, store)


def test_retrieve_returns_parent_context(tmp_path, fake_embedder):
    r = _setup(tmp_path, fake_embedder)
    # FakeEmbedder 是 hash 确定性的：用与某叶子块向量化文本一致的查询保证命中
    query = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    blocks = r.retrieve("kb1", query, top_k=3)
    assert blocks
    top = blocks[0]
    # small-to-big：返回的是父块全文，包含叶子块之外的兄弟内容
    assert "连续工作满两年" in top.text
    assert "在编在岗" in top.text
    assert top.doc_name == "补贴办法.md"
    assert "第一章" in top.heading_path


def test_parent_dedup(tmp_path, fake_embedder):
    """同一父块下多个叶子命中时，父块只出现一次。"""
    r = _setup(tmp_path, fake_embedder)
    query = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"
    blocks = r.retrieve("kb1", query, top_k=10)
    paths = [b.heading_path for b in blocks]
    assert len(paths) == len(set(paths))


def test_assemble_truncates_oversized_parent_windowed_on_leaf(tmp_path, fake_embedder):
    """D6：父块全文超过 max_parent_chars 时，_assemble 以命中叶子文本在父块中
    首次出现的位置为中心截窗，而不是简单头部截断——否则命中叶子在尾部时窗口
    会截不到叶子内容，答案就丢了关键上下文。截断处加 … 标记。"""
    from kbase.rag.retriever import Retriever

    factory = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kb1", name="库"))
        s.commit()

    leaf_text = "关键叶子命中内容——司局级住宿费标准为每晚六百五十元整。"
    padding_before = "填充文字。" * 1200      # 约 6000 字符，把叶子推到父块尾部
    parent_text = padding_before + leaf_text
    assert len(parent_text) > 6000          # 确认父块确实超过测试用例名称里的 6000 字

    from kbase.models import Chunk
    with factory() as s:
        s.add(Chunk(id="parent1", doc_id="doc1", kb_id="kb1", parent_id=None,
                    heading_path="补贴办法 > 第一章", text=parent_text, is_leaf=False))
        s.add(Chunk(id="leaf1", doc_id="doc1", kb_id="kb1", parent_id="parent1",
                    heading_path="补贴办法 > 第一章", text=leaf_text, is_leaf=True))
        from kbase.models import Document
        s.add(Document(id="doc1", kb_id="kb1", filename="补贴办法.md",
                       content_hash="h1", status="ready"))
        s.commit()

    store = ChromaStore(persist_dir=str(tmp_path / "chroma"))
    r = Retriever(factory, fake_embedder, store, max_parent_chars=4000)
    blocks = r._assemble([("leaf1", 1.0)], top_k=1)
    assert len(blocks) == 1
    text = blocks[0].text
    assert len(text) <= 4000 + 20            # 上限 + 少量 … 标记余量
    assert leaf_text in text
    assert text.startswith("…")
    # T17：不传 context_budget = 不限预算，这一块**没有**截断标记（标记是非
    # 字段属性，只有预算分支才会挂上，见 retriever 模块顶部注释）。
    assert getattr(blocks[0], "truncated_note", None) is None


# ---------------- T17 上下文预算 ----------------


def _kb_with_doc(tmp_path, fake_embedder, kb_id, md, chunk_size=700, name=None):
    """建一个库并摄取一份 Markdown，返回 (factory, store, retriever)。

    KB 配置里显式给 chunk_size/chunk_overlap：KB 级分块覆盖只在配置里显式给键
    时生效（见 ingest/pipeline._chunker_for），单测建库也要按同一口径写。"""
    factory = make_session_factory(f"sqlite:///{tmp_path}/{kb_id}.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id=kb_id, name=kb_id,
                            config=json.dumps({"chunk_size": chunk_size,
                                               "chunk_overlap": 0})))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / f"chroma-{kb_id}"))
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=chunk_size,
                                                       chunk_overlap=0),
                              fake_embedder, store, tmp_path / f"files-{kb_id}")
    f = tmp_path / f"{kb_id}.md"
    f.write_text(md, encoding="utf-8")
    pipeline.ingest_file(kb_id, f, f"{kb_id}.md")
    return factory, store, Retriever(factory, fake_embedder, store)


def _leaves(factory, kb_id=None):
    """库里全部叶子块（按 id 排序，保证断言稳定）。"""
    with factory() as s:
        q = s.query(Chunk).filter(Chunk.is_leaf.is_(True))
        if kb_id is not None:
            q = q.filter(Chunk.kb_id == kb_id)
        return q.order_by(Chunk.id).all()


def _leaf_query(leaf) -> str:
    """命中某叶子块的查询串。FakeEmbedder 按文本 hash 确定性出向量，所以查询
    必须与该叶子块的**嵌入文本**逐字一致；表格块摄取时的嵌入文本是行线性化
    文本（见 kbase/embed_text.py），不是块正文。"""
    text = leaf.text
    if leaf.layout:
        layout = json.loads(leaf.layout)
        if layout.get("kind") == "table" and layout.get("linearized"):
            text = layout["linearized"]
    return f"{leaf.heading_path}\n{text}"


def _big_table(rows=30, table_name="收费标准") -> str:
    """一张远超测试用预算的表（30 行 ≈ 700+ 字符）。"""
    body = "\n".join(f"| 项目{i} | {600 + i} |" for i in range(rows))
    return f"# {table_name}\n\n| 项目 | 金额 |\n| --- | --- |\n{body}\n"


def test_budget_none_keeps_output_byte_identical(tmp_path, fake_embedder):
    """验收契约：context_budget=None 时检索输出与 T17 之前**逐字节一致**。

    证明方式不是"看代码没改到"，而是两条路径对拍：
    ① 不传 context_budget 与显式传 None 的整份 asdict 序列化结果相同；
    ② 组装层同参数（只有 ordered/top_k 的旧调用形态）结果相同。
    并顺带钉死那个被上一次失败尝试踩坏的具体数值——同一条查询、同一份语料下
    首块分数必须仍然是 0.9999999933774619（不是 1.0，也不是别的近似值），
    且 block 字典里**没有** truncated/truncated_note 这类新键。"""
    r = _setup(tmp_path, fake_embedder)
    query = "补贴办法.md > 补贴办法 > 第一章 申领条件\n连续工作满两年可申领住房补贴。"

    plain = [asdict(b) for b in r.retrieve("kb1", query, top_k=3)]
    unbudgeted = RetrievalStrategy(use_keyword=True, use_rerank=False,
                                   rewrite_mode="off", candidates=20,
                                   context_budget=None)
    explicit = [asdict(b) for b in r.retrieve("kb1", query, top_k=3,
                                              strategy=unbudgeted)]
    assert plain == explicit
    # 分数逐字不变（T17 之前实测值；上一次失败的尝试把它算成了别的数）
    assert plain[0]["score"] == 0.9999999933774619
    # 序列化形状不变：既有键一个不少、新键一个不多
    assert set(plain[0]) == {"doc_id", "doc_name", "heading_path", "text",
                             "snippet", "score", "page", "kb_id"}
    # 组装层：旧调用形态（只有 ordered/top_k）与显式 None 同参同输出
    with r._sf() as s:
        ordered = [(c.id, 1.0) for c in s.query(Chunk)
                   .filter(Chunk.is_leaf.is_(True)).all()]
    old = r._assemble(ordered, 10)
    assert [asdict(b) for b in old] == [asdict(b) for b in
                                        r._assemble(ordered, 10, context_budget=None)]
    # 没有预算 → 没有截断标记（属性压根不存在，不是"碰巧是 False"）
    assert all(not hasattr(b, "truncated_note") for b in old)


def test_budget_degrades_table_to_header_and_hit_rows(tmp_path, fake_embedder):
    """表格块：整表放不进预算 → 退化为「表头 + 命中行 + 共 N 行（已截断）」，
    并在块上挂截断标记（前端据此在引用旁标「已截断」）。"""
    factory, _store, r = _kb_with_doc(tmp_path, fake_embedder, "kb1",
                                      _big_table(rows=30))
    leaves = _leaves(factory)
    assert len(leaves) == 1
    leaf = leaves[0]
    assert len(leaf.text) > 400                    # 整表确实远超下面的预算

    budget = 120
    blocks = r._assemble([(leaf.id, 1.0)], top_k=1, context_budget=budget)
    assert len(blocks) == 1
    text = blocks[0].text
    # 退化格式：表头保留（列名↔值的绑定不能丢）+ 至少一行命中数据 + 说明行
    assert text.startswith("| 项目 | 金额 |")
    assert "| 项目0 | 600 |" in text
    assert text.endswith("共 30 行（已截断）")
    assert len(text) <= budget                     # 预算是硬上限
    assert text.count("\n") < 30                   # 没有把 30 行原样带出
    assert blocks[0].truncated_note == "共 30 行（已截断）"
    # 引用载荷带上「已截断」标记，snippet 仍是原始叶子文本（不伪造内容）
    from kbase.rag.generator import Generator

    class _LLM:
        pass

    cite = Generator(_LLM(), min_score=0.0).citations(blocks)
    assert cite[0]["truncated"] is True
    assert cite[0]["truncated_note"] == "共 30 行（已截断）"
    assert cite[0]["snippet"] == leaf.text
    # 对照：不设预算时同一块是整表、无标记（说明上面的退化确实由预算触发）
    whole = r._assemble([(leaf.id, 1.0)], top_k=1)[0]
    assert getattr(whole, "truncated_note", None) is None
    assert "已截断" not in whole.text


def test_budget_boundary_exact_and_exhausted(tmp_path, fake_embedder):
    """边界：预算恰好用尽 / 差一个字符 / 只够第一块。

    两块不同章节各自成父块（父块去重不会把它们并成一个）。预算按 text 字符数
    累计，所以 L1+L2 恰好让两块都完整进来、L1+L2-1 让第二块被截窗，
    只给 L1 时第二块直接不进上下文（停止条件=预算耗尽 或 top_k）。"""
    md = "# 文件\n## 一\n" + "甲" * 300 + "\n## 二\n" + "乙" * 300 + "\n"
    factory, _store, r = _kb_with_doc(tmp_path, fake_embedder, "kb1", md,
                                      chunk_size=400)
    leaves = _leaves(factory)
    assert len(leaves) == 2
    ordered = [(leaves[0].id, 2.0), (leaves[1].id, 1.0)]
    l1 = len(r._assemble([ordered[0]], top_k=1)[0].text)
    l2 = len(r._assemble([ordered[1]], top_k=1)[0].text)
    assert l1 > 0 and l2 > 0

    exact = r._assemble(ordered, top_k=2, context_budget=l1 + l2)
    assert [len(b.text) for b in exact] == [l1, l2]
    assert all(getattr(b, "truncated_note", None) is None for b in exact)

    # 差一个字符：第二块放不下 → 按剩余预算截窗，且带上截断说明
    tight = r._assemble(ordered, top_k=2, context_budget=l1 + l2 - 1)
    assert len(tight) == 2
    assert tight[0].text == exact[0].text
    assert getattr(tight[0], "truncated_note", None) is None
    assert len(tight[1].text) <= l2 - 1
    assert tight[1].truncated_note                   # 标记只落在被裁的那块上

    # 预算只够第一块：第二块根本不进上下文
    only_first = r._assemble(ordered, top_k=2, context_budget=l1)
    assert len(only_first) == 1
    assert only_first[0].text == exact[0].text

    # 预算再大也不会超过 top_k 个父块（既有契约不放松）
    assert len(r._assemble(ordered, top_k=1, context_budget=99999)) == 1


def test_budget_split_per_kb_in_retrieve_multi(tmp_path, fake_embedder):
    """多库规则（T17）：预算是**按库平分**的（每库 budget // len(kb_ids)），
    不是各库共享一整份；合并后总量不超过配置预算，单库不超份额。"""
    fan = _kb_with_doc(tmp_path, fake_embedder, "kbA",
                       "# 甲文\n## 章\n" + "甲" * 600 + "\n")
    fbn = _kb_with_doc(tmp_path, fake_embedder, "kbB",
                       "# 乙文\n## 章\n" + "乙" * 600 + "\n")
    fa, sa, ra = fan
    fb, sb, rb = fbn
    # 两个库装进同一个 DB/向量库，才能共用一个 Retriever
    factory = make_session_factory(f"sqlite:///{tmp_path}/multi.sqlite")
    with factory() as s:
        s.add(KnowledgeBase(id="kbA", name="甲库"))
        s.add(KnowledgeBase(id="kbB", name="乙库"))
        s.commit()
    store = ChromaStore(persist_dir=str(tmp_path / "chroma-multi"))
    pipeline = IngestPipeline(factory, StructureChunker(chunk_size=700,
                                                       chunk_overlap=0),
                              fake_embedder, store, tmp_path / "files-multi")
    for kb, text in (("kbA", "# 甲文\n## 章\n" + "甲" * 600 + "\n"),
                     ("kbB", "# 乙文\n## 章\n" + "乙" * 600 + "\n")):
        p = tmp_path / f"{kb}.md"
        p.write_text(text, encoding="utf-8")
        pipeline.ingest_file(kb, p, f"{kb}.md")
    assert fa is not None and fb is not None and sa is not None and sb is not None
    assert ra is not None and rb is not None
    rm = Retriever(factory, fake_embedder, store)

    budget = 400
    got: dict[str, int] = {}
    real_retrieve = rm.retrieve      # 先抓住原方法，否则 spy 会递归调用自己

    def spy_retrieve(kb_id, query, top_k, debug=False, strategy=None, filters=None):
        got[kb_id] = strategy.context_budget
        return real_retrieve(kb_id, query, top_k, debug, strategy, filters)

    rm.retrieve = spy_retrieve
    merged = rm.retrieve_multi(["kbA", "kbB"], "甲", top_k=5,
                               strategy=RetrievalStrategy(
                                   use_keyword=True, use_rerank=False,
                                   rewrite_mode="off", candidates=20,
                                   context_budget=budget))
    # 每库拿到的是 budget // 2（向下取整），不是整份 400
    assert got == {"kbA": budget // 2, "kbB": budget // 2}
    assert sum(len(b.text) for b in merged) <= budget
    assert len(merged) <= 5                     # top_k 契约不放松
