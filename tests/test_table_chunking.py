"""表格能力最终版（M6）：原子分块、大表按行组切+表头重复、行线性化、
嵌入/关键词文本组成收敛、端到端单元格检索与编辑重线性化。"""
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from kbase.embed_text import embed_input, keyword_input
from kbase.plugins.chunkers.structure import (
    StructureChunker, linearize_table, parse_table, split_segments, split_table,
)
from tests.test_api import CFG, FakeLLM

TABLE = (
    "| 设备名称 | 型号 | 采购金额 |\n"
    "| --- | --- | --- |\n"
    "| 高速扫描仪 | SC-9000 | 3.5万元 |\n"
    "| 工业相机 | CAM-200 | 1.2万元 |\n"
)

# 表格置于独立小节：真实文档里表格几乎总有自己的标题，父块随之分离，
# small-to-big 组装才会把表格块作为独立 block 返回（单章节共父块会被
# top_k=父块数 的去重收敛成一个 block，那是 small-to-big 的既有语义）。
DOC = (f"# 采购清单\n\n本季度采购情况汇总如下。\n\n"
       f"## 设备明细表\n\n{TABLE}\n"
       f"## 验收说明\n\n以上设备均已验收合格。\n")


# ---------------- 纯函数 ----------------


def test_split_segments_isolates_table():
    segs = split_segments(f"前置说明。\n\n{TABLE}\n结尾说明。")
    kinds = [k for k, _ in segs]
    assert kinds == ["text", "table", "text"]
    assert "SC-9000" in segs[1][1]


def test_parse_and_linearize():
    header, rows = parse_table(TABLE)
    assert header == ["设备名称", "型号", "采购金额"]
    lin = linearize_table(header, rows)
    assert "设备名称=高速扫描仪；型号=SC-9000；采购金额=3.5万元。" in lin
    assert "设备名称=工业相机" in lin


# GLM-OCR（扫描件）产出 HTML <table>，不是 Markdown 管道表格
HTML_TABLE = (
    '<table border="1">'
    "<tr><td>城市级别</td><td>住宿上限</td></tr>"
    "<tr><td>一线城市</td><td>每晚500元</td></tr>"
    "<tr><td>二线城市</td><td>每晚350元</td></tr>"
    "</table>"
)


def test_parse_html_table_from_ocr():
    header, rows = parse_table(HTML_TABLE)
    assert header == ["城市级别", "住宿上限"]
    assert rows == [["一线城市", "每晚500元"], ["二线城市", "每晚350元"]]
    assert "城市级别=二线城市；住宿上限=每晚350元。" in linearize_table(header, rows)


def test_html_table_segmented_and_chunked():
    """GLM-OCR 输出的 HTML 表格同样走表格感知分块（正文归一为 Markdown）。"""
    doc = f"## 差旅住宿标准表\n\n{HTML_TABLE}\n"
    segs = split_segments(doc)
    assert ("table", HTML_TABLE) in [(k, c) for k, c in segs]
    chunks = StructureChunker(chunk_size=200, chunk_overlap=0).chunk(doc, "标准.md")
    tbl = [c for c in chunks if c.parent_id and c.meta.get("layout")]
    assert len(tbl) == 1
    # 正文重建为 Markdown 表格（前端/LLM 统一按 Markdown 阅读）
    assert tbl[0].text.splitlines()[0].startswith("| 城市级别 |")
    assert "住宿上限=每晚350元" in tbl[0].meta["layout"]["linearized"]


def test_split_table_repeats_header_per_group():
    """超预算的大表按行组切，每组都带表头（保住列名↔值绑定）。"""
    rows = "\n".join(f"| 设备{i} | X-{i} | {i}万元 |" for i in range(1, 41))
    big = "| 设备名称 | 型号 | 金额 |\n| --- | --- | --- |\n" + rows + "\n"
    pieces = split_table(big, chunk_size=300)
    assert len(pieces) > 1
    for body, linearized in pieces:
        assert body.splitlines()[0].startswith("| 设备名称 |")   # 每组重复表头
        assert "设备名称=" in linearized
    # 所有数据行都在，无丢失
    all_bodies = "\n".join(b for b, _ in pieces)
    assert all(f"X-{i}" in all_bodies for i in range(1, 41))


def test_chunker_table_atomicity_and_meta():
    chunks = StructureChunker(chunk_size=200, chunk_overlap=0).chunk(DOC, "采购.md")
    leaves = [c for c in chunks if c.parent_id is not None]
    table_leaves = [c for c in leaves if c.meta.get("layout")]
    assert len(table_leaves) == 1                       # 小表=单原子块
    t = table_leaves[0]
    assert t.text.splitlines()[0].startswith("| 设备名称 |")   # 正文是完整表格
    assert "SC-9000" in t.text and "CAM-200" in t.text
    assert t.meta["layout"]["kind"] == "table"
    assert "型号=SC-9000" in t.meta["layout"]["linearized"]
    # 普通文本叶子不带 layout，且不含表格行（表格没被混切进文本块）
    for c in leaves:
        if not c.meta.get("layout"):
            assert "| ---" not in c.text


def test_embed_and_keyword_input_use_linearized_for_tables():
    layout = {"kind": "table", "linearized": "型号=SC-9000。"}
    e = embed_input(None, "doc > 表", "|原始|表格|", layout)
    k = keyword_input("doc > 表", "|原始|表格|", layout)
    assert "型号=SC-9000" in e and "|原始|表格|" not in e
    assert "型号=SC-9000" in k and "|原始|表格|" not in k
    # 无 layout：沿用原文（既有行为）
    assert "普通文本" in embed_input("增强句", "h", "普通文本")


# ---------------- 端到端 ----------------


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def test_table_cell_retrievable_and_snippet_is_full_table(tmp_path, fake_embedder):
    """单元格值经关键词路可命中（线性化进了 BM25），命中块正文是**完整表格**
    （表头在场）——这正是'表格切碎后答非所问'问题的验收标准。"""
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("采购.md", DOC.encode("utf-8"), "text/markdown"))])
    assert c.get(f"/api/kb/{kb_id}/documents").json()[0]["status"] == "ready"

    # 用唯一单元格值查询：关键词路只会命中表格块（线性化文本被 BM25 索引），
    # 假向量的噪声排名不影响判定（RRF 融合下双路命中者必然登顶）。
    blocks = c.post(f"/api/kb/{kb_id}/search",
                    json={"query": "SC-9000 采购金额", "top_k": 3}).json()["blocks"]
    assert blocks, "线性化文本应让单元格值可被召回"
    hit = next((b for b in blocks if "SC-9000" in b["snippet"]), None)
    assert hit is not None, f"snippets: {[b['snippet'][:30] for b in blocks]}"
    assert hit["snippet"].splitlines()[0].startswith("| 设备名称 |")   # snippet=完整表格


def test_edit_table_chunk_relinearizes(tmp_path, fake_embedder):
    """编辑表格块：改单元格值后重算线性化，新值立即可被关键词路命中。"""
    c = _client(tmp_path, fake_embedder)
    kb_id = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
           files=[("files", ("采购.md", DOC.encode("utf-8"), "text/markdown"))])
    doc_id = c.get(f"/api/kb/{kb_id}/documents").json()[0]["id"]
    table_chunk = next(
        i for i in c.get(f"/api/documents/{doc_id}/chunks").json()["items"]
        if i["is_leaf"] and (i.get("layout") or {}).get("kind") == "table")

    new_text = table_chunk["text"].replace("SC-9000", "SC-9999PLUS")
    r = c.put(f"/api/chunks/{table_chunk['id']}", json={"text": new_text}).json()
    assert "型号=SC-9999PLUS" in r["layout"]["linearized"]

    blocks = c.post(f"/api/kb/{kb_id}/search",
                    json={"query": "SC-9999PLUS", "top_k": 3}).json()["blocks"]
    assert any("SC-9999PLUS" in b["snippet"] for b in blocks)
