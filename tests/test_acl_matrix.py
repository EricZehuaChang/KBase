"""T02/G09：库级 ACL 与 API Key scope 的读/写入口矩阵。

09-14 代码走查发现：`kbase/api/routes/kb.py` 全文件没有任何 `kb_acl.can_access`
调用，只有 `list_kb` 做了 ACL + scope 过滤；凡是**以 doc_id / chunk_id 为参数**
的端点（列表、全文、原件、附图、分块、审核、重试）都只校验角色。id 是 uuid4
不可枚举，风险被部分缓解，但 id 会出现在引用/分享/日志里——拿到 id 就能读他库
文档。评测集（evals）与生成任务（jobs）的 `_guard_kb` 只查 ACL，没叠加受限 key
的 scope；连接器（connectors）与分享链接管理组同理。

本文件是**行为契约**：无授权 viewer 与受限 API Key 对库外资源一律 404，
且读写一视同仁（这里没有"静默空集"的既有契约——那是检索/问答路径的约定，
见 tests/test_apikey_scope.py）。授权内的资源必须仍然可用（防"守卫写成全拒"）。
"""

import pytest
from fastapi.testclient import TestClient

from kbase.api.main import create_app
from tests.test_api import CFG, FakeLLM

DOC_A = "# 财务制度\n\n本库的差旅报销标准是每晚 500 元。\n"
DOC_B = "# 禁区制度\n\n他库的差旅报销标准是每晚 900 元。\n"


@pytest.fixture
def app_on(tmp_path, fake_embedder, monkeypatch):
    monkeypatch.setenv("KBASE_ADMIN_PASSWORD", "admin-pw")
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    return create_app(config_path=cfg, embedder=fake_embedder,
                      llms={"fake": FakeLLM()}, reranker=False, auth="on")


def _login(username, password, app):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


class Fixture:
    """两库两文档：kb_a（**公开库**，无 grant 行）+ kb_b（授权给 other，
    对其他人收紧）。公开库用于所有"授权内仍可用"的正例——它对登录用户、
    未设 scope 的 key、白名单内的受限 key 都可访问，一个正例覆盖三种 actor。
    kb_b 是主测对象：viewer1/其他 editor/受限 key 对它都无权。"""

    def __init__(self, admin, app):
        self.app = app
        self.admin = admin
        self.kb_a = admin.post("/api/kb", json={"name": "允许库"}).json()["id"]
        self.kb_b = admin.post("/api/kb", json={"name": "禁区库"}).json()["id"]
        self.doc_a = self._upload(self.kb_a, "a.md", DOC_A)
        self.doc_b = self._upload(self.kb_b, "b.md", DOC_B)
        self.other = admin.post("/api/users", json={
            "username": "other", "role": "viewer", "password": "other-pw"}).json()
        self.viewer = admin.post("/api/users", json={
            "username": "viewer1", "role": "viewer", "password": "viewer-pw"}).json()
        # 只收紧 kb_b：授权集合里只有 other —— viewer1/未授权的 editor/受限
        # key（user_id 为 NULL）对 kb_b 一律无权。kb_a 保持公开（无 grant）。
        admin.put(f"/api/kb/{self.kb_b}/grants",
                  json={"user_ids": [self.other["id"]]})
        self.viewer_client = _login("viewer1", "viewer-pw", app)
        # 无授权 editor：写类端点会被 require_editor 放行到业务层，才测得到
        # 库级守卫（viewer 先被角色矩阵挡 403，看不见守卫是否生效）
        admin.post("/api/users", json={"username": "ed0", "role": "editor",
                                       "password": "ed0-pw"})
        self.unauthorized_editor = _login("ed0", "ed0-pw", app)
        self.chunk_a = self._chunk_id(self.doc_a)
        self.chunk_b = self._chunk_id(self.doc_b)
        # 路由覆盖守卫用到的"他库资源 id"
        from kbase.jobs.store import create_job
        self.set_id_b = admin.post(
            f"/api/kb/{self.kb_b}/eval-sets",
            json={"name": "他库集", "cases": [
                {"question": "报销", "expect_text": "900"}]}).json()["id"]
        self.run_id_b = "run-not-exist"
        self.job_id_b = create_job(app.state.svc.sf, kb_id=self.kb_b,
                                   type="digest",
                                   params={"doc_ids": [self.doc_b]},
                                   provider=None)["id"]
        conn = admin.post(f"/api/kb/{self.kb_b}/connectors",
                          json={"type": "feishu", "name": "他库连接器",
                                "source": "http://example.invalid/wiki"})
        self.connector_id_b = (conn.json()["id"] if conn.status_code == 200
                               else "conn-not-exist")

    def _upload(self, kb, name, text):
        """上传并返回落库的 doc_id（摄取是同步 bg task，响应返回时已完成，
        见 routes/kb.py 的 _ingest_batch 注释），故直接按文件名查列表即可。"""
        r = self.admin.post(f"/api/kb/{kb}/documents",
                            files={"files": (name, text.encode("utf-8"),
                                             "text/markdown")})
        assert r.status_code == 200, r.text
        docs = self.admin.get(f"/api/kb/{kb}/documents").json()
        matches = [d for d in docs if d["filename"] == name]
        assert matches, f"{name} 未落库: {docs}"
        return matches[0]["id"]

    def _chunk_id(self, doc_id):
        r = self.admin.get(f"/api/documents/{doc_id}/chunks")
        assert r.status_code == 200, r.text
        return r.json()["items"][0]["id"]

    def grant(self, kb_id, *user_ids):
        """把 kb 收紧到指定用户集合（空=恢复公开）。"""
        r = self.admin.put(f"/api/kb/{kb_id}/grants",
                           json={"user_ids": list(user_ids)})
        assert r.status_code == 200, r.text


def _scoped_key(admin, scope, *, role="viewer", name="scoped"):
    r = admin.post("/api/settings/api-keys",
                   json={"name": name, "role": role, "scope_kb_ids": scope})
    assert r.status_code == 200, r.text
    return r.json()["key"]


@pytest.fixture
def fx(app_on):
    admin = _login("admin", "admin-pw", app_on)
    return Fixture(admin, app_on)


def _doc_reads(doc_id):
    """viewer 门槛的读类端点（以 doc_id 为参数）。"""
    return [
        ("GET", f"/api/documents/{doc_id}/content", {}),
        ("GET", f"/api/documents/{doc_id}/original", {}),
        ("GET", f"/api/documents/{doc_id}/chunks", {}),
        ("GET", f"/api/documents/{doc_id}/images/nope.png", {}),
    ]


def _doc_writes(doc_id):
    """editor 门槛的写类端点（以 doc_id 为参数）。"""
    return [
        ("POST", f"/api/documents/{doc_id}/retry", {}),
        ("PUT", f"/api/documents/{doc_id}/review", {"json": {"markdown": "# x"}}),
    ]


def _call(client, method, path, kwargs):
    return client.request(method, path, **kwargs)


def test_unauthorized_viewer_doc_read_endpoints_404(fx):
    """无授权 viewer 拿 kb_b 的 doc_id：读类端点全部 404（修前全文/原件/分块
    都是 200——id 从引用/日志泄漏即等于内容泄漏）。"""
    reads = [
        ("GET", f"/api/documents/{fx.doc_b}/content", {}),
        ("GET", f"/api/documents/{fx.doc_b}/original", {}),
        ("GET", f"/api/documents/{fx.doc_b}/chunks", {}),
        ("GET", f"/api/documents/{fx.doc_b}/images/nope.png", {}),
        ("GET", f"/api/kb/{fx.kb_b}/documents", {}),
    ]
    for method, path, kwargs in reads:
        r = _call(fx.viewer_client, method, path, kwargs)
        assert r.status_code == 404, f"{method} {path} → {r.status_code}"


def test_unauthorized_editor_doc_write_endpoints_404(fx):
    """无授权 **editor** 拿 kb_b 的 doc_id/chunk_id：写类端点全部 404
    （viewer 会被 require_editor 先挡 403，所以写类要用 editor 才测得到守卫；
    修前 retry/review 是 200/409，chunk 停用是 200）。"""
    fx.admin.post("/api/users", json={"username": "ed9", "role": "editor",
                                      "password": "ed9-pw"})
    ed9 = _login("ed9", "ed9-pw", fx.app)
    for method, path, kwargs in _doc_writes(fx.doc_b):
        r = _call(ed9, method, path, kwargs)
        assert r.status_code == 404, f"{method} {path} → {r.status_code}"
    assert ed9.put(f"/api/chunks/{fx.chunk_b}",
                   json={"enabled": False}).status_code == 404
    assert ed9.post(f"/api/kb/{fx.kb_b}/retry-ocr").status_code == 404
    # 授权库的写操作仍然可用
    assert ed9.post(f"/api/kb/{fx.kb_a}/retry-ocr").status_code == 200


def test_authorized_viewer_still_works(fx):
    """授权内的资源不受影响——守卫不能写成"全拒"。"""
    assert fx.viewer_client.get(
        f"/api/documents/{fx.doc_a}/content").status_code == 200
    assert fx.viewer_client.get(
        f"/api/documents/{fx.doc_a}/chunks").status_code == 200
    assert fx.viewer_client.get(
        f"/api/kb/{fx.kb_a}/documents").status_code == 200
    assert fx.viewer_client.post(
        f"/api/kb/{fx.kb_a}/search", json={"query": "报销"}).status_code == 200
    # viewer 对写类端点 403 是既有角色矩阵（与本次守卫无关），确认没被守卫
    # 改写成 404——角色不足与资源无权是两种语义，不能混。
    assert fx.viewer_client.put(
        f"/api/chunks/{fx.chunk_a}", json={"enabled": True}).status_code == 403


def test_scoped_key_doc_endpoints_404(fx):
    """受限 API Key（白名单只有 kb_a）拿 kb_b 的 doc_id/chunk_id：读类 404、
    写类 404。读类用 viewer key、写类用 editor key——viewer 会被 require_editor
    先挡 403，测不到守卫。

    把 kb_b 恢复公开后重跑一遍：此时 ACL 放行，404 只可能来自 scope——
    证明 scope 这条闸门独立生效，而不是被 ACL 顺带挡住（假绿）。"""
    viewer_key = _scoped_key(fx.admin, [fx.kb_a], name="scoped-reader")
    cv = TestClient(fx.app, headers={"Authorization": f"Bearer {viewer_key}"})
    for method, path, kwargs in _doc_reads(fx.doc_b):
        r = _call(cv, method, path, kwargs)
        assert r.status_code == 404, f"{method} {path} → {r.status_code}"
    assert cv.get(f"/api/kb/{fx.kb_b}/documents").status_code == 404
    # 白名单内的库仍可用
    assert cv.get(f"/api/documents/{fx.doc_a}/content").status_code == 200

    editor_key = _scoped_key(fx.admin, [fx.kb_a], role="editor",
                             name="scoped-writer")
    ce = TestClient(fx.app, headers={"Authorization": f"Bearer {editor_key}"})
    for method, path, kwargs in _doc_writes(fx.doc_b):
        r = _call(ce, method, path, kwargs)
        assert r.status_code == 404, f"{method} {path} → {r.status_code}"
    assert ce.put(f"/api/chunks/{fx.chunk_b}",
                  json={"enabled": False}).status_code == 404
    assert ce.post(f"/api/kb/{fx.kb_b}/retry-ocr").status_code == 404
    # 白名单内的库写操作仍可用
    assert ce.post(f"/api/kb/{fx.kb_a}/retry-ocr").status_code == 200

    # kb_b 恢复公开（ACL 全放行）→ 受限 key 仍必须被 scope 挡住
    fx.grant(fx.kb_b)
    assert cv.get(f"/api/documents/{fx.doc_b}/content").status_code == 404
    assert cv.get(f"/api/kb/{fx.kb_b}/documents").status_code == 404
    assert ce.put(f"/api/chunks/{fx.chunk_b}",
                  json={"enabled": False}).status_code == 404
    # 同一时刻不设 scope 的 key 能读（对照：排除"库本身不可读"）
    open_key = _scoped_key(fx.admin, None, name="open-after-public")
    co = TestClient(fx.app, headers={"Authorization": f"Bearer {open_key}"})
    assert co.get(f"/api/documents/{fx.doc_b}/content").status_code == 200


def test_scoped_admin_key_doc_endpoints_404(fx):
    """受限 **admin** key：kb_acl 对 admin 豁免 ACL，若只判 ACL 就会放行。"""
    key = _scoped_key(fx.admin, [fx.kb_a], role="admin", name="scoped-admin")
    c = TestClient(fx.app, headers={"Authorization": f"Bearer {key}"})
    assert c.get(f"/api/documents/{fx.doc_b}/content").status_code == 404
    assert c.get(f"/api/documents/{fx.doc_a}/content").status_code == 200


def test_unscoped_key_unchanged(fx):
    """不设 scope 的 key：行为与升级前一致——公开库可读，而 ACL 收紧的库
    仍然只有被授权的人能读（key 的 user_id 为 NULL，不在 kb_b 授权集合里）。"""
    key = _scoped_key(fx.admin, None, name="open")
    c = TestClient(fx.app, headers={"Authorization": f"Bearer {key}"})
    assert c.get(f"/api/documents/{fx.doc_a}/content").status_code == 200
    assert c.get(f"/api/documents/{fx.doc_b}/content").status_code == 404


def test_evals_endpoints_respect_scope_and_acl(fx):
    """评测集入口：受限 key 不得读写白名单外库的评测集；无授权 viewer
    拿不到他库评测集（editor 门槛先挡 viewer 属既有设计，这里用受限
    editor key + 无授权 editor 验证）。"""
    set_b = fx.admin.post(f"/api/kb/{fx.kb_b}/eval-sets",
                          json={"name": "他库集",
                                "cases": [{"question": "报销", "expect_text": "900"}]})
    assert set_b.status_code == 200, set_b.text
    set_id = set_b.json()["id"]

    key = _scoped_key(fx.admin, [fx.kb_a], role="editor", name="scoped-editor")
    c = TestClient(fx.app, headers={"Authorization": f"Bearer {key}"})
    assert c.get(f"/api/kb/{fx.kb_b}/eval-sets").status_code == 404
    assert c.post(f"/api/eval-sets/{set_id}/run", json={}).status_code == 404
    assert c.get(f"/api/eval-sets/{set_id}/runs").status_code == 404
    assert c.delete(f"/api/eval-sets/{set_id}").status_code == 404

    # kb_b 恢复公开（ACL 放行）后重跑：404 只可能来自 scope
    fx.grant(fx.kb_b)
    assert c.get(f"/api/kb/{fx.kb_b}/eval-sets").status_code == 404
    assert c.post(f"/api/eval-sets/{set_id}/run", json={}).status_code == 404
    assert c.get(f"/api/eval-sets/{set_id}/runs").status_code == 404
    assert c.delete(f"/api/eval-sets/{set_id}").status_code == 404
    # 对照：不设 scope 的 editor key 能读（ACL 已放行）
    open_key = _scoped_key(fx.admin, None, role="editor", name="open-eval")
    co = TestClient(fx.app, headers={"Authorization": f"Bearer {open_key}"})
    assert co.get(f"/api/kb/{fx.kb_b}/eval-sets").status_code == 200

    # 无授权 editor（kb_b 授权集合里只有 other）：再次收紧后同样 404
    fx.grant(fx.kb_b, fx.other["id"])
    fx.admin.post("/api/users", json={"username": "ed2", "role": "editor",
                                      "password": "ed2-pw"})
    ed2 = _login("ed2", "ed2-pw", fx.app)
    assert ed2.get(f"/api/kb/{fx.kb_b}/eval-sets").status_code == 404
    # 授权/公开库仍可用
    assert ed2.post(f"/api/kb/{fx.kb_a}/eval-sets",
                    json={"name": "自己的", "cases": []}).status_code in (200, 422)


def test_jobs_endpoints_respect_acl_and_scope(fx):
    """生成任务：建任务/读任务/取产物都要过 kb 的 ACL+scope（修前只校验角色）。"""
    from kbase.jobs.store import create_job
    sf = fx.app.state.svc.sf
    job = create_job(sf, kb_id=fx.kb_b, type="digest",
                     params={"doc_ids": [fx.doc_b]}, provider=None)

    # 无授权 editor
    fx.admin.post("/api/users", json={"username": "ed3", "role": "editor",
                                      "password": "ed3-pw"})
    ed3 = _login("ed3", "ed3-pw", fx.app)
    assert ed3.get(f"/api/jobs?kb_id={fx.kb_b}").status_code == 404
    assert ed3.get(f"/api/jobs/{job['id']}").status_code == 404
    assert ed3.get(f"/api/jobs/{job['id']}/artifact").status_code == 404
    assert ed3.post("/api/jobs", json={"kb_id": fx.kb_b, "type": "digest",
                                       "params": {}}).status_code == 404

    # 受限 key（白名单只有 kb_a）
    key = _scoped_key(fx.admin, [fx.kb_a], role="editor", name="scoped-jobs")
    c = TestClient(fx.app, headers={"Authorization": f"Bearer {key}"})
    assert c.get(f"/api/jobs?kb_id={fx.kb_b}").status_code == 404
    assert c.get(f"/api/jobs/{job['id']}").status_code == 404
    assert c.get(f"/api/jobs/{job['id']}/artifact").status_code == 404
    # 白名单内的库仍可用
    assert c.get(f"/api/jobs?kb_id={fx.kb_a}").status_code == 200

    # kb_b 恢复公开（ACL 放行）→ 受限 key 仍被 scope 挡住；不设 scope 的
    # editor key 则能读到 job（对照，排除"job 本身不可读"）
    fx.grant(fx.kb_b)
    assert c.get(f"/api/jobs/{job['id']}").status_code == 404
    open_key = _scoped_key(fx.admin, None, role="editor", name="open-jobs")
    co = TestClient(fx.app, headers={"Authorization": f"Bearer {open_key}"})
    assert co.get(f"/api/jobs/{job['id']}").status_code == 200


def test_connectors_respect_acl_and_scope(fx):
    """连接器：查/建都要过 kb 的 ACL+scope（修前只校验 kb 存在 + 角色）。"""
    created = fx.admin.post(f"/api/kb/{fx.kb_b}/connectors",
                            json={"type": "feishu", "name": "他库连接器",
                                  "source": "http://example.invalid/wiki"})
    assert created.status_code in (200, 409), created.text

    fx.admin.post("/api/users", json={"username": "ed4", "role": "editor",
                                      "password": "ed4-pw"})
    ed4 = _login("ed4", "ed4-pw", fx.app)
    assert ed4.get(f"/api/kb/{fx.kb_b}/connectors").status_code == 404

    key = _scoped_key(fx.admin, [fx.kb_a], role="editor", name="scoped-conn")
    c = TestClient(fx.app, headers={"Authorization": f"Bearer {key}"})
    assert c.get(f"/api/kb/{fx.kb_b}/connectors").status_code == 404

    # kb_b 恢复公开（ACL 放行）→ 仍被 scope 挡住；无 scope 的 key 能读
    fx.grant(fx.kb_b)
    assert c.get(f"/api/kb/{fx.kb_b}/connectors").status_code == 404
    open_key = _scoped_key(fx.admin, None, role="editor", name="open-conn")
    co = TestClient(fx.app, headers={"Authorization": f"Bearer {open_key}"})
    assert co.get(f"/api/kb/{fx.kb_b}/connectors").status_code == 200


def test_share_link_creation_respects_acl_and_scope(fx):
    """建分享链接：主库与 extra_kb_ids 副库都要过 ACL+scope——否则拿到 id
    的人可以把无权库挂进免登录链接，绕开 ACL 把内容发出去（修前只校验存在）。"""
    fx.admin.post("/api/users", json={"username": "ed5", "role": "editor",
                                      "password": "ed5-pw"})
    ed5 = _login("ed5", "ed5-pw", fx.app)
    # ed5 对 kb_b 无授权：主库路径 404、副库路径也 404
    assert ed5.post(f"/api/kb/{fx.kb_b}/share-links",
                    json={"name": "偷偷分享"}).status_code == 404
    assert ed5.post(f"/api/kb/{fx.kb_a}/share-links",
                    json={"name": "夹带", "extra_kb_ids": [fx.kb_b]}).status_code == 404
    assert ed5.post(f"/api/kb/{fx.kb_a}/share-links",
                    json={"name": "正常", "extra_kb_ids": [fx.kb_a]}).status_code == 200

    # 受限 key：白名单外的主库/副库都 404
    key = _scoped_key(fx.admin, [fx.kb_a], role="editor", name="scoped-share")
    c = TestClient(fx.app, headers={"Authorization": f"Bearer {key}"})
    assert c.post(f"/api/kb/{fx.kb_b}/share-links",
                  json={"name": "越权"}).status_code == 404
    assert c.post(f"/api/kb/{fx.kb_a}/share-links",
                  json={"name": "夹带", "extra_kb_ids": [fx.kb_b]}).status_code == 404
    # 自己白名单内的库可建链接，且列表接口同样按 scope 挡
    ok = c.post(f"/api/kb/{fx.kb_a}/share-links", json={"name": "正常"})
    assert ok.status_code == 200
    assert c.get(f"/api/kb/{fx.kb_b}/share-links").status_code == 404

    # kb_b 恢复公开（ACL 放行）→ 受限 key 建链接/拉列表仍被 scope 挡住；
    # 无 scope 的 editor key 能建（对照，排除"链接功能本身不可用"）
    fx.grant(fx.kb_b)
    assert c.post(f"/api/kb/{fx.kb_b}/share-links",
                  json={"name": "越权"}).status_code == 404
    open_key = _scoped_key(fx.admin, None, role="editor", name="open-share")
    co = TestClient(fx.app, headers={"Authorization": f"Bearer {open_key}"})
    assert co.post(f"/api/kb/{fx.kb_b}/share-links",
                   json={"name": "公开库"}).status_code == 200


# ---------------------------------------------------------------------------
# 路由覆盖守卫：新增"以资源 id 为参数"的端点时，必须挂上 KbGuard
# ---------------------------------------------------------------------------
# 上面各条测的是**今天已存在**的端点。真正会复发的是"以后有人加一个新端点、
# 忘了守卫"——所以这里从应用自己的 OpenAPI 里枚举路由，对每个家族逐个发一次
# "无授权调用方"请求，断言不会拿到 2xx。新增端点若忘了守卫，这条立刻红，
# 而且报错信息里直接给出方法+路径。

# 家族前缀 → 用哪个无授权客户端打（viewer 覆盖不了的写类端点用 editor）
_ID_SCOPED_FAMILIES = {
    "/api/documents/": "editor",     # 读类 viewer 也过，写类要 editor
    "/api/chunks/": "editor",
    "/api/jobs": "editor",
    "/api/connectors/": "editor",
    "/api/eval-sets/": "editor",
    "/api/eval-runs/": "editor",
}

# 有意豁免：这些路径虽然带 id，但语义上不以"某个库的资源"为授权单位
_EXEMPT = {
    "/api/share/{token}",                          # token 即授权（公开组）
    "/api/share/{token}/query",
    "/api/share/{token}/images/{doc_id}/{filename}",
    "/api/i18n/{lang}",
}


def test_all_id_scoped_routes_are_guarded(fx):
    """从 OpenAPI 枚举所有"以资源 id 为参数"的端点，逐个用无授权调用方请求，
    断言一律不是 2xx（既有的 404 守卫、或角色矩阵的 403）。

    这条会在**新增端点忘记守卫**时失败——T02/G09 的复发闸门。
    """
    paths = fx.app.openapi()["paths"]
    checked = 0
    failures = []
    for path, ops in sorted(paths.items()):
        if path in _EXEMPT or "{" not in path:
            continue
        family = next((f for f in _ID_SCOPED_FAMILIES if path.startswith(f)), None)
        if family is None:
            continue
        # 把路径参数替换成 kb_b 家族的真实 id（他库资源）
        concrete = (path.replace("{doc_id}", fx.doc_b)
                        .replace("{chunk_id}", fx.chunk_b)
                        .replace("{set_id}", fx.set_id_b)
                        .replace("{run_id}", fx.run_id_b)
                        .replace("{job_id}", fx.job_id_b)
                        .replace("{connector_id}", fx.connector_id_b)
                        .replace("{filename}", "nope.png"))
        assert "{" not in concrete, f"{path} 的路径参数没在夹具里登记"
        caller = fx.unauthorized_editor if _ID_SCOPED_FAMILIES[family] == "editor" \
            else fx.viewer_client
        for method in ops:
            checked += 1
            r = _call(caller, method.upper(), concrete, {})
            if 200 <= r.status_code < 300:
                failures.append(f"{method.upper()} {concrete} → {r.status_code}")
    assert checked >= 12, f"枚举到的端点数异常（{checked}），夹具或前缀表可能要更新"
    assert not failures, (
        "以下以资源 id 为参数的端点对无授权调用方返回了成功——"
        "检查是否漏挂 KbGuard（kbase/api/guards.py）：\n  "
        + "\n  ".join(failures))
