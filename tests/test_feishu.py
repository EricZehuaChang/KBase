"""飞书连接器一期：凭据 CRUD 脱敏、blocks→markdown 转换（标题/列表/表格）、
wiki 树遍历层级注入 heading_path、未配凭据 409、端到端导入可检索。
飞书网络层全部打桩，不出网。"""
import pytest
from fastapi.testclient import TestClient

import kbase.feishu as feishu
from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM


def _client(tmp_path, fake_embedder):
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="off")
    return TestClient(app)


def test_credentials_crud_and_masking(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    assert c.get("/api/settings/feishu").json()["configured"] is False

    c.put("/api/settings/feishu",
          json={"app_id": "cli_abc123", "app_secret": "supersecret9876"})
    st = c.get("/api/settings/feishu").json()
    assert st == {"configured": True, "app_id": "cli_abc123",
                  "secret_hint": "…9876"}
    assert "supersecret" not in str(st)

    assert c.delete("/api/settings/feishu").json()["ok"] is True
    assert c.delete("/api/settings/feishu").status_code == 404


def test_blocks_to_markdown_core_types():
    blocks = [
        {"block_id": "p", "block_type": 1, "children": ["h", "t", "b1", "o1", "o2", "tb"]},
        {"block_id": "h", "block_type": 3,
         "heading1": {"elements": [{"text_run": {"content": "报销章节"}}]}},
        {"block_id": "t", "block_type": 2,
         "text": {"elements": [{"text_run": {"content": "先提交行程单。"}}]}},
        {"block_id": "b1", "block_type": 12,
         "bullet": {"elements": [{"text_run": {"content": "附发票"}}]}},
        {"block_id": "o1", "block_type": 13,
         "ordered": {"elements": [{"text_run": {"content": "第一步"}}]}},
        {"block_id": "o2", "block_type": 13,
         "ordered": {"elements": [{"text_run": {"content": "第二步"}}]}},
        # 2x2 表格：cells 平铺 children
        {"block_id": "tb", "block_type": 31,
         "table": {"property": {"row_size": 2, "column_size": 2}},
         "children": ["c1", "c2", "c3", "c4"]},
        {"block_id": "c1", "block_type": 32, "children": ["c1t"]},
        {"block_id": "c1t", "block_type": 2,
         "text": {"elements": [{"text_run": {"content": "项目"}}]}},
        {"block_id": "c2", "block_type": 32, "children": ["c2t"]},
        {"block_id": "c2t", "block_type": 2,
         "text": {"elements": [{"text_run": {"content": "标准"}}]}},
        {"block_id": "c3", "block_type": 32, "children": ["c3t"]},
        {"block_id": "c3t", "block_type": 2,
         "text": {"elements": [{"text_run": {"content": "住宿"}}]}},
        {"block_id": "c4", "block_type": 32, "children": ["c4t"]},
        {"block_id": "c4t", "block_type": 2,
         "text": {"elements": [{"text_run": {"content": "500元"}}]}},
    ]
    md = feishu.blocks_to_markdown(blocks, base_heading_level=2)
    assert "### 报销章节" in md          # 1级标题下移2级
    assert "先提交行程单。" in md
    assert "- 附发票" in md
    assert "1. 第一步" in md and "2. 第二步" in md
    assert "| 项目 | 标准 |" in md and "| 住宿 | 500元 |" in md
    assert "|---|---|" in md


def _jpeg_bytes(w=320, h=240) -> bytes:
    import io as _io

    from PIL import Image
    buf = _io.BytesIO()
    Image.frombytes(
        "RGB", (w, h),
        bytes((i * 37 + j * 11) % 256 for j in range(h) for i in range(w * 3))
    ).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


@pytest.fixture
def feishu_stub(monkeypatch):
    """打桩飞书 API：两层 wiki 树（指南目录 > 报销制度docx，含插图块）。"""
    monkeypatch.setattr(feishu, "_get_token", lambda a, b: "fake-token")

    def fake_children(token, space_id, parent=None):
        if parent is None:
            return [{"node_token": "n1", "title": "员工指南", "has_child": True,
                     "obj_type": "folder"}]
        if parent == "n1":
            return [{"node_token": "n2", "title": "报销制度", "has_child": False,
                     "obj_type": "docx", "obj_token": "doc-1"}]
        return []

    monkeypatch.setattr(feishu, "list_children", fake_children)
    monkeypatch.setattr(feishu, "fetch_doc_blocks", lambda t, d: [
        {"block_id": "p", "block_type": 1, "children": ["h", "t", "img"]},
        {"block_id": "h", "block_type": 3,
         "heading1": {"elements": [{"text_run": {"content": "审批流程"}}]}},
        {"block_id": "t", "block_type": 2,
         "text": {"elements": [{"text_run": {"content":
             "差旅报销走OA两级审批，住宿上限每晚500元。"}}]}},
        {"block_id": "img", "block_type": 27,
         "image": {"token": "media-token-1"}},
    ])
    monkeypatch.setattr(feishu, "download_media",
                        lambda t, mt, doc_token=None: _jpeg_bytes())


def test_import_requires_credentials(tmp_path, fake_embedder):
    c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    r = c.post(f"/api/kb/{kb}/import-feishu", json={"source": "7123456"})
    assert r.status_code == 409          # 前端据此弹凭据输入


@pytest.fixture
def feishu_stub_deep(monkeypatch):
    """打桩深层 wiki 树（复刻产线bug现场）：凌动驾驶舱 > 用户指南 > v6.0.0 >
    驾驶舱使用手册(docx)。祖先链占掉 1-3 级标题、文档标题占 4 级，文内标题
    被下推到 h5——此前 chunker 只认 h1-h4，文内标题进不了 heading_path，
    图片的章节锚永远匹配不上（产线 0/165 张可匹配）。"""
    monkeypatch.setattr(feishu, "_get_token", lambda a, b: "fake-token")

    tree = {
        None: [("n1", "凌动驾驶舱", True, "folder", None)],
        "n1": [("n2", "用户指南", True, "folder", None)],
        "n2": [("n3", "v6.0.0", True, "folder", None)],
        "n3": [("n4", "驾驶舱使用手册", False, "docx", "doc-deep")],
    }

    def fake_children(token, space_id, parent=None):
        return [{"node_token": nt, "title": t, "has_child": hc,
                 "obj_type": ot, "obj_token": obj}
                for nt, t, hc, ot, obj in tree.get(parent, [])]

    monkeypatch.setattr(feishu, "list_children", fake_children)
    monkeypatch.setattr(feishu, "fetch_doc_blocks", lambda t, d: [
        {"block_id": "p", "block_type": 1, "children": ["h", "t", "img"]},
        {"block_id": "h", "block_type": 3,
         "heading1": {"elements": [{"text_run": {"content": "登录驾驶舱"}}]}},
        {"block_id": "t", "block_type": 2,
         "text": {"elements": [{"text_run": {"content":
             "打开浏览器访问驾驶舱地址，输入账号密码完成登录。"}}]}},
        {"block_id": "img", "block_type": 27,
         "image": {"token": "media-deep-1"}},
    ])
    monkeypatch.setattr(feishu, "download_media",
                        lambda t, mt, doc_token=None: _jpeg_bytes())


def test_deep_hierarchy_headings_and_images(tmp_path, fake_embedder,
                                            feishu_stub_deep):
    """回归：4 层 wiki 链下文内标题（h5）必须进 heading_path，图片章节锚
    才能命中（bug：chunker 只认 h1-h4 → 附图恒空）。"""
    c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.put("/api/settings/feishu", json={"app_id": "cli_x", "app_secret": "s"})

    r = c.post(f"/api/kb/{kb}/import-feishu", json={"source": "7999999"})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1

    # 文内章节标题必须出现在 heading_path（h5 级）
    hits = c.post(f"/api/kb/{kb}/search",
                  json={"query": "登录 账号密码", "top_k": 5}).json()["blocks"]
    assert hits
    assert any("登录驾驶舱" in h["heading_path"] for h in hits), \
        [h["heading_path"] for h in hits]

    # 问答命中该章节 → 图片必须附出
    import json as _json
    citations = []
    with c.stream("POST", f"/api/kb/{kb}/query",
                  json={"question": "如何登录驾驶舱", "top_k": 5}) as resp:
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event == "citations":
                citations = _json.loads(line[5:].strip())
                break
    with_img = [ci for ci in citations if ci.get("images")]
    assert with_img, f"深层级文档的插图应随引用附出: {citations}"


def test_import_space_end_to_end(tmp_path, fake_embedder, feishu_stub):
    c = _client(tmp_path, fake_embedder)
    kb = c.post("/api/kb", json={"name": "库"}).json()["id"]
    c.put("/api/settings/feishu",
          json={"app_id": "cli_x", "app_secret": "sec_x"})

    r = c.post(f"/api/kb/{kb}/import-feishu", json={"source": "7123456"})
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": ["报销制度.md"], "total": 1}

    docs = c.get(f"/api/kb/{kb}/documents").json()
    assert docs[0]["status"] == "ready"

    # 层级验证：wiki 路径（员工指南 > 报销制度）+ 文内章节都在 heading_path
    hits = c.post(f"/api/kb/{kb}/search",
                  json={"query": "住宿上限 500", "top_k": 5}).json()["blocks"]
    assert hits
    hp = hits[0]["heading_path"]
    assert "员工指南" in hp and "报销制度" in hp and "审批流程" in hp, hp

    # 图片同步：文档插图按章节锚落库，问答命中"审批流程"章节即附图
    import json as _json
    citations = []
    with c.stream("POST", f"/api/kb/{kb}/query",
                  json={"question": "住宿上限是多少", "top_k": 5}) as resp:
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event == "citations":
                citations = _json.loads(line[5:].strip())
                break
    flow = [ci for ci in citations if "审批流程" in ci["heading_path"]]
    assert flow and flow[0].get("images"), f"飞书插图应随引用附出: {citations}"
    img = flow[0]["images"][0]
    assert img["width"] == 320
    got = c.get(img["url"])
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/")
