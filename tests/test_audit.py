"""审计日志测试：kbase/audit.py 存储原语（write_audit/list_audit）单测，
以及端到端覆盖（登录成/败、上传、删除、查询触发审计行；GET /api/audit
分页与 admin-only 权限）。端到端部分复用 tests/test_roles.py 的用户建号
辅助函数与 CFG/FakeLLM（tests/test_api.py）。
"""
from kbase.audit import list_audit, write_audit
from kbase.db import make_session_factory
from tests.test_api import MD
from tests.test_auth import _client_on
from tests.test_roles import _add_user, _login


def _sf(tmp_path):
    return make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")


def _sf_for_app(tmp_path):
    return make_session_factory(f"sqlite:///{tmp_path}/data/kbase.sqlite")


# ---- 存储原语 ----

def test_write_audit_roundtrip(tmp_path):
    sf = _sf(tmp_path)
    write_audit(sf, actor="alice", action="POST /api/kb", resource="kb-1",
               detail={"name": "政策库"}, ip="127.0.0.1")
    result = list_audit(sf)
    assert result["total"] == 1
    row = result["items"][0]
    assert row["actor"] == "alice"
    assert row["action"] == "POST /api/kb"
    assert row["resource"] == "kb-1"
    assert '"name": "政策库"' in row["detail"]
    assert row["ip"] == "127.0.0.1"
    assert row["ts"]     # ISO 时间戳非空


def test_write_audit_detail_truncated(tmp_path):
    sf = _sf(tmp_path)
    long_detail = "x" * 5000
    write_audit(sf, actor="alice", action="query", detail=long_detail)
    row = list_audit(sf)["items"][0]
    assert len(row["detail"]) == 2000


def test_write_audit_accepts_none_resource_and_detail(tmp_path):
    sf = _sf(tmp_path)
    write_audit(sf, actor="admin", action="login_success")
    row = list_audit(sf)["items"][0]
    assert row["resource"] is None
    assert row["detail"] is None


def test_list_audit_orders_desc_and_paginates(tmp_path):
    sf = _sf(tmp_path)
    for i in range(5):
        write_audit(sf, actor="alice", action=f"action-{i}")
    result = list_audit(sf, limit=2, offset=0)
    assert result["total"] == 5
    assert [item["action"] for item in result["items"]] == ["action-4", "action-3"]
    result2 = list_audit(sf, limit=2, offset=2)
    assert [item["action"] for item in result2["items"]] == ["action-2", "action-1"]


# ---- 端到端（auth="on"）：登录成/败、mutating 请求、问答，均应落审计行 ----

def test_login_success_and_failure_both_write_audit_rows(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "wrongpass"})
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})

    sf = _sf_for_app(tmp_path)
    rows = list_audit(sf)["items"]
    actions = [r["action"] for r in rows]
    assert "login_failed" in actions
    assert "login_success" in actions
    failed = next(r for r in rows if r["action"] == "login_failed")
    success = next(r for r in rows if r["action"] == "login_success")
    assert failed["actor"] == "admin"      # 登录失败也记录尝试的用户名
    assert success["actor"] == "admin"


def test_upload_writes_audit_row_with_actor_and_resource(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    kb_id = c.post("/api/kb", json={"name": "政策库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
          files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])

    sf = _sf_for_app(tmp_path)
    rows = list_audit(sf)["items"]
    upload_row = next(r for r in rows
                      if r["action"] == "POST /api/kb/{kb_id}/documents")
    assert upload_row["actor"] == "admin"
    assert upload_row["resource"] == f"kb_id={kb_id}"


def test_delete_document_writes_audit_row(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    kb_id = c.post("/api/kb", json={"name": "政策库"}).json()["id"]
    c.post(f"/api/kb/{kb_id}/documents",
          files=[("files", ("补贴办法.md", MD.encode("utf-8"), "text/markdown"))])
    doc_id = c.get(f"/api/kb/{kb_id}/documents").json()[0]["id"]
    c.delete(f"/api/kb/{kb_id}/documents/{doc_id}")

    sf = _sf_for_app(tmp_path)
    rows = list_audit(sf)["items"]
    delete_row = next(
        r for r in rows
        if r["action"] == "DELETE /api/kb/{kb_id}/documents/{doc_id}")
    assert delete_row["actor"] == "admin"
    assert f"kb_id={kb_id}" in delete_row["resource"]
    assert f"doc_id={doc_id}" in delete_row["resource"]


def test_query_writes_audit_row_with_truncated_question(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    kb_id = c.post("/api/kb", json={"name": "政策库"}).json()["id"]
    long_question = "住房补贴申领条件到底是什么" * 20     # 远超100字
    c.post(f"/api/kb/{kb_id}/query", json={"question": long_question, "provider": "fake"})

    sf = _sf_for_app(tmp_path)
    rows = list_audit(sf)["items"]
    query_row = next(r for r in rows if r["action"] == "query")
    assert query_row["actor"] == "admin"
    assert query_row["resource"] == f"kb_id={kb_id}"
    assert query_row["detail"] == long_question[:100]


def test_forbidden_request_does_not_write_mutation_audit_row(tmp_path, fake_embedder, monkeypatch):
    """viewer 被 403 拒绝的 mutating 请求不应产生审计行——require_role 先于
    audit_mutation 依赖执行，被拒的请求根本不会走到审计钩子。"""
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    _add_user(tmp_path, "viewer1", "viewer")
    _login(c, "viewer1")
    r = c.post("/api/kb", json={"name": "不该建成"})
    assert r.status_code == 403

    sf = _sf_for_app(tmp_path)
    rows = list_audit(sf)["items"]
    assert not any(row["action"] == "POST /api/kb" for row in rows)


# ---- GET /api/audit：admin-only + 分页 ----

def test_audit_endpoint_requires_admin(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    _add_user(tmp_path, "editor1", "editor")
    _login(c, "editor1")
    assert c.get("/api/audit").status_code == 403


def test_audit_endpoint_pagination_admin(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    for i in range(3):
        c.post("/api/kb", json={"name": f"库{i}"})

    r = c.get("/api/audit", params={"limit": 2, "offset": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 3        # 至少 3 次建库 + 1 次登录成功
    assert len(body["items"]) == 2
