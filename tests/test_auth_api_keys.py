"""API Key 管理端点（admin）：POST 创建（一次性返回完整 key）/GET 列表
（不含 hash）/DELETE 吊销（软删除）。Bearer 通道校验已在 G2 的 deps.py
落地（见 test_auth_deps.py），这里补的是设置页 CRUD 与吊销后 401 的
端到端贯通（经由这几个新端点，而不是直接操作 DB）。
"""
from fastapi.testclient import TestClient

from tests.test_auth import _client_on


def _create_key(c, name="mcp-key", role="viewer"):
    return c.post("/api/settings/api-keys", json={"name": name, "role": role})


def _login_admin(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    return app, c


def test_create_api_key_returns_full_key_once(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    r = _create_key(c, name="mcp-key", role="viewer")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "mcp-key"
    assert body["role"] == "viewer"
    assert body["key"].startswith("kbase_ak_")
    assert "id" in body


def test_list_api_keys_never_exposes_hash(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    created = _create_key(c, name="mcp-key", role="viewer").json()
    r = c.get("/api/settings/api-keys")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    item = items[0]
    assert item["id"] == created["id"]
    assert item["name"] == "mcp-key"
    assert item["role"] == "viewer"
    assert item["revoked"] is False
    assert "created_at" in item
    random_part = created["key"][len("kbase_ak_"):]
    assert item["prefix"] == random_part[:8]
    assert "key" not in item
    assert "key_hash" not in item
    assert "hash" not in item


def test_revoke_api_key_marks_revoked_in_list(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    created = _create_key(c, name="mcp-key", role="viewer").json()
    r = c.delete(f"/api/settings/api-keys/{created['id']}")
    assert r.status_code == 200
    items = c.get("/api/settings/api-keys").json()
    assert items[0]["revoked"] is True


def test_revoke_unknown_api_key_404(tmp_path, fake_embedder, monkeypatch):
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    r = c.delete("/api/settings/api-keys/does-not-exist")
    assert r.status_code == 404


def test_api_key_full_lifecycle_create_use_revoke_401(tmp_path, fake_embedder, monkeypatch):
    """key 全生命周期：建→用（Bearer 通道过 GET /api/kb）→吊销→再用返回 401。
    Bearer 通道校验本身在 G2 的 deps.py 落地（见 test_auth_deps.py 的
    单元测试），这里走真实的设置 API 端到端贯通一次。"""
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    created = _create_key(c, name="mcp-key", role="viewer").json()
    full_key = created["key"]

    anon = TestClient(app)   # 独立客户端：不带管理员的会话 Cookie，只用 Bearer
    ok = anon.get("/api/kb", headers={"Authorization": f"Bearer {full_key}"})
    assert ok.status_code == 200

    c.delete(f"/api/settings/api-keys/{created['id']}")

    revoked = anon.get("/api/kb", headers={"Authorization": f"Bearer {full_key}"})
    assert revoked.status_code == 401


def test_create_api_key_invalid_role_422(tmp_path, fake_embedder, monkeypatch):
    """伪角色应被 pydantic Literal 校验拒为 422——否则伪角色写进 api_keys 后，
    Bearer 通道 actor 的 role 会在 deps 的 _ROLE_RANK 下标处 500。"""
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    r = c.post("/api/settings/api-keys", json={"name": "bad-key", "role": "x"})
    assert r.status_code == 422


def test_api_key_role_propagates_to_require_role(tmp_path, fake_embedder, monkeypatch):
    """Bearer actor 的角色来自 key 的 role——viewer key 打设置端点应 403，
    admin key 应放行（探测 G2/G3 wiring：actor dict 的 role 必须真的来自
    api_keys.role，而不是被写死成某个常量）。"""
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    viewer_key = _create_key(c, name="viewer-key", role="viewer").json()["key"]
    admin_key = _create_key(c, name="admin-key", role="admin").json()["key"]

    anon = TestClient(app)
    r_viewer = anon.get("/api/settings/api-keys",
                        headers={"Authorization": f"Bearer {viewer_key}"})
    assert r_viewer.status_code == 403
    r_admin = anon.get("/api/settings/api-keys",
                       headers={"Authorization": f"Bearer {admin_key}"})
    assert r_admin.status_code == 200
