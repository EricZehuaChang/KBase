"""会话归属过滤测试（auth="on"）：M5-1 F2——会话预鉴权而生、原是全局的，
补 user_id 后要验证"各自的会话互相隔离、历史遗留 NULL 会话两边可见、admin
不享有特权"这几条产品取舍（见 kbase/conversations.py _visible_filter 的
设计注释）。复用 tests/test_roles.py 的 _add_user/_login/_make_kb_as_admin
（同一批 auth="on" 端到端测试基础设施，不重复造轮子）。
"""
from fastapi.testclient import TestClient

from kbase.db import make_session_factory
from kbase.models import Conversation
from tests.test_roles import _add_user, _login, _make_kb_as_admin


def test_sessions_isolated_between_two_users(tmp_path, fake_embedder, monkeypatch):
    app, kb_id, _doc_id = _make_kb_as_admin(tmp_path, fake_embedder, monkeypatch)
    _add_user(tmp_path, "viewerA", "viewer")
    _add_user(tmp_path, "viewerB", "viewer")
    c_a = _login(TestClient(app), "viewerA")
    c_b = _login(TestClient(app), "viewerB")

    conv_a = c_a.post("/api/conversations", json={"kb_id": kb_id}).json()
    conv_b = c_b.post("/api/conversations", json={"kb_id": kb_id}).json()

    ids_a = {c["id"] for c in c_a.get("/api/conversations", params={"kb_id": kb_id}).json()["items"]}
    ids_b = {c["id"] for c in c_b.get("/api/conversations", params={"kb_id": kb_id}).json()["items"]}
    assert conv_a["id"] in ids_a and conv_a["id"] not in ids_b
    assert conv_b["id"] in ids_b and conv_b["id"] not in ids_a

    # B 看不到 A 的会话消息/不能对 A 的会话发起问答——统一 404，不用 403
    # （403 会暴露"这个 conv_id 确实存在，只是不归你"，见 store 层注释）。
    assert c_b.get(f"/api/conversations/{conv_a['id']}/messages").status_code == 404
    assert c_b.post(f"/api/conversations/{conv_a['id']}/query",
                    json={"question": "x"}).status_code == 404
    # A 查自己的会话消息正常
    assert c_a.get(f"/api/conversations/{conv_a['id']}/messages").status_code == 200


def test_legacy_null_owner_session_visible_to_all(tmp_path, fake_embedder, monkeypatch):
    """模拟鉴权改造前（M5-1 之前）建的会话：迁移后 user_id 是 NULL，不倒推
    补归属人，两个不同的登录用户都应该能看到/能读消息（宽松历史数据兜底，
    见 _visible_filter 设计注释）。"""
    app, kb_id, _doc_id = _make_kb_as_admin(tmp_path, fake_embedder, monkeypatch)
    sf = make_session_factory(f"sqlite:///{tmp_path}/data/kbase.sqlite")
    with sf() as s:
        s.add(Conversation(id="legacy-conv-1", kb_id=kb_id, title="遗留会话"))
        s.commit()

    _add_user(tmp_path, "viewerC", "viewer")
    _add_user(tmp_path, "viewerD", "viewer")
    c_c = _login(TestClient(app), "viewerC")
    c_d = _login(TestClient(app), "viewerD")

    ids_c = {c["id"] for c in c_c.get("/api/conversations", params={"kb_id": kb_id}).json()["items"]}
    ids_d = {c["id"] for c in c_d.get("/api/conversations", params={"kb_id": kb_id}).json()["items"]}
    assert "legacy-conv-1" in ids_c
    assert "legacy-conv-1" in ids_d
    assert c_c.get("/api/conversations/legacy-conv-1/messages").status_code == 200
    assert c_d.get("/api/conversations/legacy-conv-1/messages").status_code == 200


def test_admin_does_not_see_others_sessions(tmp_path, fake_embedder, monkeypatch):
    """隐私默认：admin 不享有"看所有人会话"的特权，同样只能看自己名下 +
    历史遗留会话（见 _visible_filter 设计注释——审计日志兜底追溯，不用靠
    admin 翻会话内容补运维能力）。"""
    app, kb_id, _doc_id = _make_kb_as_admin(tmp_path, fake_embedder, monkeypatch)
    _add_user(tmp_path, "viewerE", "viewer")
    c_e = _login(TestClient(app), "viewerE")
    conv_e = c_e.post("/api/conversations", json={"kb_id": kb_id}).json()

    c_admin = _login(TestClient(app), "admin", "adminpass123")
    ids_admin = {c["id"] for c in c_admin.get("/api/conversations", params={"kb_id": kb_id}).json()["items"]}
    assert conv_e["id"] not in ids_admin
    assert c_admin.get(f"/api/conversations/{conv_e['id']}/messages").status_code == 404


def test_delete_respects_ownership_filter(tmp_path, fake_embedder, monkeypatch):
    app, kb_id, _doc_id = _make_kb_as_admin(tmp_path, fake_embedder, monkeypatch)
    _add_user(tmp_path, "viewerF", "viewer")
    _add_user(tmp_path, "viewerG", "viewer")
    c_f = _login(TestClient(app), "viewerF")
    c_g = _login(TestClient(app), "viewerG")
    conv_f = c_f.post("/api/conversations", json={"kb_id": kb_id}).json()

    assert c_g.delete(f"/api/conversations/{conv_f['id']}").status_code == 404
    assert c_f.delete(f"/api/conversations/{conv_f['id']}").status_code == 200
    # 删过之后连本人也查不到了（真删除，不是软删）
    assert c_f.get(f"/api/conversations/{conv_f['id']}/messages").status_code == 404


def test_rename_conversation(tmp_path, fake_embedder, monkeypatch):
    app, kb_id, _doc_id = _make_kb_as_admin(tmp_path, fake_embedder, monkeypatch)
    _add_user(tmp_path, "viewerH", "viewer")
    _add_user(tmp_path, "viewerI", "viewer")
    c_h = _login(TestClient(app), "viewerH")
    c_i = _login(TestClient(app), "viewerI")
    conv_h = c_h.post("/api/conversations", json={"kb_id": kb_id}).json()

    r = c_h.put(f"/api/conversations/{conv_h['id']}", json={"title": "  新标题  "})
    assert r.status_code == 200
    assert r.json()["title"] == "新标题"          # 首尾空白已裁剪

    # 别人的会话改不了（404，不泄漏存在性）
    assert c_i.put(f"/api/conversations/{conv_h['id']}",
                   json={"title": "抢标题"}).status_code == 404

    # 空标题（裁剪后为空）拒绝
    assert c_h.put(f"/api/conversations/{conv_h['id']}",
                   json={"title": "   "}).status_code == 422
