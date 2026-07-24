"""免登录分享链接（对标 #1）：建/列/撤销（editor 门槛）、公开 meta、
免登录问答全流程（SSE 事件序列与登录端一致）、撤销即 404、防枚举。
auth=on 真实鉴权——公开端点必须在无 Cookie 下可用。"""
import json

import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM


@pytest.fixture
def app_on(tmp_path, fake_embedder, monkeypatch):
    monkeypatch.setenv("KBASE_ADMIN_PASSWORD", "admin-pw")
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    return create_app(config_path=cfg, embedder=fake_embedder,
                      llms={"fake": FakeLLM()}, reranker=False, auth="on")


def _login(app, username="admin", password="admin-pw"):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def _prepare_kb_with_doc(admin) -> str:
    kb = admin.post("/api/kb", json={"name": "分享库"}).json()["id"]
    r = admin.post(f"/api/kb/{kb}/documents",
                   files=[("files", ("报销.md",
                                     "# 报销制度\n住宿上限每晚500元。".encode(),
                                     "text/markdown"))])
    assert r.status_code == 200, r.text
    return kb


def test_share_link_full_flow(app_on):
    admin = _login(app_on)
    kb = _prepare_kb_with_doc(admin)

    # 建链接（provider 绑定在建链接侧；这里用默认=None）
    link = admin.post(f"/api/kb/{kb}/share-links",
                      json={"name": "官网客服"}).json()
    assert link["token"] and link["name"] == "官网客服"

    # 列表可见完整 token（复制分发用）
    rows = admin.get(f"/api/kb/{kb}/share-links").json()
    assert [r["id"] for r in rows] == [link["id"]]

    # 公开端点：全新无 Cookie 客户端
    anon = TestClient(app_on)
    meta = anon.get(f"/api/share/{link['token']}")
    assert meta.status_code == 200 and meta.json()["kb_name"] == "分享库"

    # 免登录问答：SSE 事件序列与登录端一致（citations→token*→done）
    events = []
    citations = []
    with anon.stream("POST", f"/api/share/{link['token']}/query",
                     json={"question": "住宿上限是多少"}) as resp:
        assert resp.status_code == 200
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
                events.append(event)
            elif line.startswith("data:") and event == "citations" and not citations:
                citations = json.loads(line[5:].strip())
    assert "citations" in events and "done" in events
    assert citations and "报销" in citations[0]["doc_name"]

    # 撤销 → 公开端点立即 404
    assert admin.delete(f"/api/share-links/{link['id']}").json()["ok"] is True
    assert anon.get(f"/api/share/{link['token']}").status_code == 404
    assert anon.post(f"/api/share/{link['token']}/query",
                     json={"question": "x"}).status_code == 404


def test_share_link_permissions_and_enumeration(app_on):
    admin = _login(app_on)
    kb = _prepare_kb_with_doc(admin)
    admin.post("/api/users", json={"username": "viewer1", "role": "viewer",
                                   "password": "pw123456"})
    viewer = _login(app_on, "viewer1", "pw123456")

    # viewer 不能建分享链接（editor 门槛）
    assert viewer.post(f"/api/kb/{kb}/share-links",
                       json={"name": "x"}).status_code == 403
    # 未知 token 一律 404（不泄露存在性）
    anon = TestClient(app_on)
    assert anon.get("/api/share/no-such-token-xxxx").status_code == 404


def test_share_image_public_access(app_on, tmp_path):
    """附图免登录直链：token 校验 + 文档必须属于链接绑定的库 + 防穿越。"""
    admin = _login(app_on)
    kb = _prepare_kb_with_doc(admin)
    doc_id = admin.get(f"/api/kb/{kb}/documents").json()[0]["id"]
    # 落一张假图（端点只管文件系统与归属校验，不查 DocumentImage 行）
    img_dir = tmp_path / "data" / "files" / doc_id / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    (img_dir / "t.png").write_bytes(b"\x89PNG-fake")

    link = admin.post(f"/api/kb/{kb}/share-links", json={"name": "x"}).json()
    anon = TestClient(app_on)
    r = anon.get(f"/api/share/{link['token']}/images/{doc_id}/t.png")
    assert r.status_code == 200 and r.content == b"\x89PNG-fake"

    # 别的库的文档：404（不越权出图）
    kb2 = admin.post("/api/kb", json={"name": "另一库"}).json()["id"]
    link2 = admin.post(f"/api/kb/{kb2}/share-links", json={"name": "y"}).json()
    assert anon.get(
        f"/api/share/{link2['token']}/images/{doc_id}/t.png").status_code == 404
    # 路径穿越拦截
    assert anon.get(
        f"/api/share/{link['token']}/images/{doc_id}/..%2Fcontent.md"
    ).status_code == 404


def _stream_citations(anon, token, question):
    """跑一次匿名问答，返回 (事件名列表, citations)。"""
    events, citations = [], []
    with anon.stream("POST", f"/api/share/{token}/query",
                     json={"question": question}) as resp:
        assert resp.status_code == 200, resp.read()
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
                events.append(event)
            elif line.startswith("data:") and event == "citations" and not citations:
                citations = json.loads(line[5:].strip())
    return events, citations


def test_share_link_multi_kb(app_on, tmp_path):
    """多库联查分享：建链接绑多库（副库校验存在）→ meta 报全量库名 →
    匿名问答跨库命中副库文档 → 副库附图可出 → 删副库=缩范围不死链。"""
    admin = _login(app_on)
    kb1 = _prepare_kb_with_doc(admin)                      # 报销.md
    kb2 = admin.post("/api/kb", json={"name": "差旅库"}).json()["id"]
    r = admin.post(f"/api/kb/{kb2}/documents",
                   files=[("files", ("差旅.md",
                                     "# 差旅规定\n机票需提前三天预订。".encode(),
                                     "text/markdown"))])
    assert r.status_code == 200, r.text

    # 副库不存在 → 建链接即 404（不留脏引用）
    assert admin.post(f"/api/kb/{kb1}/share-links",
                      json={"name": "x", "extra_kb_ids": ["no-such-kb"]}
                      ).status_code == 404

    link = admin.post(f"/api/kb/{kb1}/share-links",
                      json={"name": "联查分享", "extra_kb_ids": [kb2]}).json()
    assert link["kb_ids"] == [kb1, kb2]
    # 管理列表带 kb_names（显示联查范围）
    rows = admin.get(f"/api/kb/{kb1}/share-links").json()
    assert rows[0]["kb_names"] == ["分享库", "差旅库"]

    anon = TestClient(app_on)
    meta = anon.get(f"/api/share/{link['token']}").json()
    assert meta["kb_name"] == "分享库"                     # 主库名（向后兼容）
    assert meta["kb_names"] == ["分享库", "差旅库"]

    # 跨库检索：副库文档命中（问差旅问题，答案引用应含 差旅.md）
    events, citations = _stream_citations(anon, link["token"], "机票需要提前几天预订")
    assert "citations" in events and "done" in events
    assert any("差旅" in c["doc_name"] for c in citations), citations

    # 副库文档的附图免登录可出（回答可能引用副库文档）
    doc2 = admin.get(f"/api/kb/{kb2}/documents").json()[0]["id"]
    img_dir = tmp_path / "data" / "files" / doc2 / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    (img_dir / "t.png").write_bytes(b"\x89PNG-kb2")
    assert anon.get(
        f"/api/share/{link['token']}/images/{doc2}/t.png").status_code == 200

    # 删副库：链接不死（主库还在），范围静默缩为单库；已删库附图 404
    assert admin.delete(f"/api/kb/{kb2}").status_code == 200
    meta2 = anon.get(f"/api/share/{link['token']}").json()
    assert meta2["kb_names"] == ["分享库"]
    ev2, _ = _stream_citations(anon, link["token"], "住宿上限是多少")
    assert "done" in ev2
    assert anon.get(
        f"/api/share/{link['token']}/images/{doc2}/t.png").status_code == 404


def test_advanced_ui_switch(app_on):
    """viewer 高级界面开关：默认关；admin 可开；me 按角色/开关给出单一判断源。"""
    admin = _login(app_on)
    u = admin.post("/api/users", json={"username": "front.user", "role": "viewer",
                                       "password": "pw123456"}).json()
    assert u["advanced_ui"] is False

    viewer = _login(app_on, "front.user", "pw123456")
    assert viewer.get("/api/auth/me").json()["advanced_ui"] is False

    admin.put(f"/api/users/{u['id']}", json={"advanced_ui": True})
    assert viewer.get("/api/auth/me").json()["advanced_ui"] is True
    # editor/admin 恒开
    assert admin.get("/api/auth/me").json()["advanced_ui"] is True
