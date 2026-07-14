"""角色矩阵端到端测试（auth="on"）：spec §3 表按路由抽查——viewer 只读+问答
可过、内容管理被拒；editor 内容管理可过、settings 被拒；admin 全通。
复用 tests/test_auth.py 的 _client_on 辅助（auth="on" 的 create_app）与
tests/test_api.py 的 CFG/FakeLLM/MD。
"""
import uuid

from kbase.auth import security
from kbase.db import make_session_factory
from kbase.models import User
from tests.test_api import MD
from tests.test_auth import _client_on


def _add_user(tmp_path, username: str, role: str, password: str = "pw123456") -> None:
    sf = make_session_factory(f"sqlite:///{tmp_path}/data/kbase.sqlite")
    with sf() as s:
        s.add(User(id=str(uuid.uuid4()), username=username,
                   password_hash=security.hash_password(password),
                   role=role, disabled=False))
        s.commit()


def _login(client, username: str, password: str = "pw123456"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return client


def _make_kb_as_admin(tmp_path, fake_embedder, monkeypatch):
    """建库+上传一份文档，供后续角色抽查用（用 admin 会话建，避免绑死在
    被测角色的权限上）。返回 (app, client_factory, kb_id, doc_id)，
    client_factory() 返回一个全新未登录的 TestClient 复用同一个 app。"""
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    _login(c, "admin", "adminpass123")
    kb_id = c.post("/api/kb", json={"name": "政策库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
          files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    doc_id = c.get(f"/api/kb/{kb_id}/documents").json()[0]["id"]
    return app, kb_id, doc_id


# ---- viewer floor ----

def test_viewer_can_read_and_query_but_not_mutate(tmp_path, fake_embedder, monkeypatch):
    app, kb_id, doc_id = _make_kb_as_admin(tmp_path, fake_embedder, monkeypatch)
    _add_user(tmp_path, "viewer1", "viewer")
    from fastapi.testclient import TestClient
    c = TestClient(app)
    _login(c, "viewer1")

    assert c.get("/api/kb").status_code == 200
    assert c.get(f"/api/kb/{kb_id}/documents").status_code == 200
    assert c.post(f"/api/kb/{kb_id}/query",
                  json={"question": "住房补贴条件", "provider": "fake"}).status_code == 200
    assert c.post(f"/api/kb/{kb_id}/search", json={"query": "住房"}).status_code == 200

    # 会话：spec 角色矩阵"问答/会话/检索/生成任务查看"整行 viewer 可过——
    # 使用端（M5-1 F2）的主力用户就是 viewer，新建/查看/重命名/删除自己的
    # 会话都应放行（M5-1 F2 修正：曾经 POST /conversations 误挂
    # require_editor，viewer 完全没法用问答页）。
    conv = c.post("/api/conversations", json={"kb_id": kb_id}).json()
    assert c.get("/api/conversations", params={"kb_id": kb_id}).status_code == 200
    assert c.put(f"/api/conversations/{conv['id']}",
                json={"title": "改个名"}).status_code == 200
    assert c.delete(f"/api/conversations/{conv['id']}").status_code == 200

    # 内容管理类 mutating 端点：viewer 应被拒
    assert c.post(f"/api/kb/{kb_id}/documents",
                  files=[("files", ("x.md", b"x", "text/markdown"))]).status_code == 403
    assert c.delete(f"/api/kb/{kb_id}/documents/{doc_id}").status_code == 403
    assert c.delete(f"/api/kb/{kb_id}").status_code == 403
    assert c.post("/api/kb", json={"name": "新库"}).status_code == 403

    # settings：viewer 应被拒
    assert c.get("/api/settings/providers").status_code == 403


# ---- editor floor ----

def test_editor_can_mutate_content_but_not_settings(tmp_path, fake_embedder, monkeypatch):
    app, kb_id, doc_id = _make_kb_as_admin(tmp_path, fake_embedder, monkeypatch)
    _add_user(tmp_path, "editor1", "editor")
    from fastapi.testclient import TestClient
    c = TestClient(app)
    _login(c, "editor1")

    assert c.post(f"/api/kb/{kb_id}/documents",
                  files=[("files", ("y.md", MD.encode("utf-8"), "text/markdown"))]).status_code == 200
    r = c.post("/api/kb", json={"name": "editor 建的库"})
    assert r.status_code == 200
    new_kb_id = r.json()["id"]
    assert c.put(f"/api/kb/{new_kb_id}/config", json={"chunk_size": 300}).status_code == 200
    assert c.delete(f"/api/kb/{new_kb_id}").status_code == 200

    # settings：editor 应被拒（admin-only）
    assert c.get("/api/settings/providers").status_code == 403
    assert c.post("/api/settings/providers", json={
        "name": "p2", "base_url": "http://x", "api_key_env": "K",
        "model": "m"}).status_code == 403

    # 审计查询：editor 应被拒
    assert c.get("/api/audit").status_code == 403


# ---- admin floor ----

def test_admin_passes_all(tmp_path, fake_embedder, monkeypatch):
    app, kb_id, doc_id = _make_kb_as_admin(tmp_path, fake_embedder, monkeypatch)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    _login(c, "admin", "adminpass123")

    assert c.get("/api/kb").status_code == 200
    assert c.post(f"/api/kb/{kb_id}/query",
                  json={"question": "住房补贴条件", "provider": "fake"}).status_code == 200
    assert c.post(f"/api/kb/{kb_id}/documents",
                  files=[("files", ("z.md", MD.encode("utf-8"), "text/markdown"))]).status_code == 200
    assert c.delete(f"/api/kb/{kb_id}/documents/{doc_id}").status_code == 200
    assert c.get("/api/settings/providers").status_code == 200
    assert c.get("/api/audit").status_code == 200
    assert c.delete(f"/api/kb/{kb_id}").status_code == 200
