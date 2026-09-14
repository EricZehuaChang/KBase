"""免登录分享链接（对标 #1）：建/列/撤销（editor 门槛）、公开 meta、
免登录问答全流程（SSE 事件序列与登录端一致）、撤销即 404、防枚举。
auth=on 真实鉴权——公开端点必须在无 Cookie 下可用。

T10 追加：有效期/访问口令/次数上限（只问答计次，原子扣次并发不越上限）、
T10 之前的存量链接 NULL=不限（行为不变）、公开端点限流（复用 T09 的 limiter
——公开端点挂在 app 级，吃不到路由级依赖，必须手动调用）。
"""
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from kbase import ratelimit
from kbase.api.main import create_app
from kbase.api.routes import share as share_routes
from kbase.models import KnowledgeBase, ShareLink
from tests.test_api import CFG, FakeLLM


@pytest.fixture(autouse=True)
def _clean_limiter():
    """T10：公开端点限流复用 T09 的模块级 limiter 单例（滑窗/当日计数跨用例
    共享），逐用例清空——否则前一个用例的请求会把后一个用例顶成 429。"""
    ratelimit.limiter.reset()
    yield
    ratelimit.limiter.reset()


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


def _stream_citations(anon, token, question, headers=None):
    """跑一次匿名问答，返回 (事件名列表, citations)。"""
    events, citations = [], []
    with anon.stream("POST", f"/api/share/{token}/query",
                     json={"question": question}, headers=headers) as resp:
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


# ---- T10：有效期 / 访问口令 / 次数上限 / 公开端点限流 -------------------------

def _visit_count(app, token):
    """库里这条链接的 visit_count（None=从未计次，老库补列后的初值）。"""
    with app.state.svc.sf() as s:
        return s.query(ShareLink).filter_by(token=token).one().visit_count


def _make_link(admin, kb, **body):
    r = admin.post(f"/api/kb/{kb}/share-links", json={"name": "T10", **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_share_link_expiry(app_on):
    """T10 有效期：过期链接的公开端点一律 404（与已撤销同码同文案，不泄露
    "存在只是过期了"）；未来日期照常可用。日期只给 YYYY-MM-DD 时按当日
    23:59:59 收口（建链接人选"今天"不该得到一条立刻失效的链接）。"""
    admin = _login(app_on)
    kb = _prepare_kb_with_doc(admin)
    past = _make_link(admin, kb, name="过期", expires_at="2000-01-01")
    assert past["expires_at"] == "2000-01-01T23:59:59"      # 日期 → 当日终点
    anon = TestClient(app_on)
    r = anon.get(f"/api/share/{past['token']}")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "error.share_link_invalid"
    assert anon.post(f"/api/share/{past['token']}/query",
                     json={"question": "x"}).status_code == 404

    future = _make_link(admin, kb, name="有效", expires_at="2999-12-31")
    assert anon.get(f"/api/share/{future['token']}").status_code == 200
    events, _ = _stream_citations(anon, future["token"], "住宿上限是多少")
    assert "done" in events


def test_share_link_password(app_on):
    """T10 访问口令：无口令/错口令 → 401（前端靠它弹口令框，而不是判死链接），
    对口令（请求头 X-Share-Password）后 meta/问答/附图全通。库里只存 bcrypt
    哈希（复用 kbase/auth/security.py），管理端列表只回 has_password。"""
    admin = _login(app_on)
    kb = _prepare_kb_with_doc(admin)
    link = _make_link(admin, kb, name="带口令", password="open-sesame")
    assert link["has_password"] is True

    # 只存哈希：既不是明文，也绝不回传给前端
    with app_on.state.svc.sf() as s:
        row = s.query(ShareLink).filter_by(token=link["token"]).one()
        assert row.password_hash.startswith("$2")           # bcrypt
        assert "open-sesame" not in row.password_hash
    listed = admin.get(f"/api/kb/{kb}/share-links").json()[0]
    assert listed["has_password"] is True
    assert "password_hash" not in listed and "password" not in listed

    anon = TestClient(app_on)
    r = anon.get(f"/api/share/{link['token']}")             # 没带口令
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "error.share_password_required"
    assert anon.get(f"/api/share/{link['token']}",
                    headers={"X-Share-Password": "wrong"}).status_code == 401
    assert anon.post(f"/api/share/{link['token']}/query",
                     json={"question": "x"},
                     headers={"X-Share-Password": "wrong"}).status_code == 401

    ok = {"X-Share-Password": "open-sesame"}
    assert anon.get(f"/api/share/{link['token']}", headers=ok).status_code == 200
    events, citations = _stream_citations(anon, link["token"], "住宿上限是多少",
                                          headers=ok)
    assert "done" in events and citations


def test_share_link_visit_cap(app_on, tmp_path):
    """T10 次数上限：**只有免登录问答计次**（meta 与附图反复取都不算），用尽
    后 meta/问答一律 404（与撤销同码）。visit_count 从 NULL 起步也能正确累计。"""
    admin = _login(app_on)
    kb = _prepare_kb_with_doc(admin)
    doc_id = admin.get(f"/api/kb/{kb}/documents").json()[0]["id"]
    img_dir = tmp_path / "data" / "files" / doc_id / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    (img_dir / "t.png").write_bytes(b"\x89PNG-t10")

    link = _make_link(admin, kb, name="两次", max_visits=2)
    anon = TestClient(app_on)
    # meta + 附图各取三次：一个都不计次（访客看一眼库名/取张图不算访问）
    for _ in range(3):
        assert anon.get(f"/api/share/{link['token']}").status_code == 200
        assert anon.get(
            f"/api/share/{link['token']}/images/{doc_id}/t.png").status_code == 200
    assert _visit_count(app_on, link["token"]) is None

    for _ in range(2):                                      # 两次问答正好用满
        events, _ = _stream_citations(anon, link["token"], "住宿上限是多少")
        assert "done" in events
    assert _visit_count(app_on, link["token"]) == 2

    r = anon.get(f"/api/share/{link['token']}")             # 用尽 → 404
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "error.share_link_invalid"
    assert anon.post(f"/api/share/{link['token']}/query",
                     json={"question": "x"}).status_code == 404
    # 但**已发出回答**里的附图仍要能出：最后一次问答刚把额度用光，它的插图
    # 若跟着 404 就是裂图（附图不计次，URL 也早随回答给出去了）
    assert anon.get(
        f"/api/share/{link['token']}/images/{doc_id}/t.png").status_code == 200
    # 管理端能看到用量（运营视角）
    listed = admin.get(f"/api/kb/{kb}/share-links").json()[0]
    assert (listed["max_visits"], listed["visit_count"]) == (2, 2)


def test_share_link_visit_cap_concurrent(app_on):
    """T10 并发扣次：上限 3，8 个并发问答只放行 3 个。

    证明点是 share_query 里那条"条件自增 + 数影响行数"的原子 UPDATE：先
    SELECT 再 UPDATE 的话，并发请求会在窗口里双双读到"还没满"而超发（本用例
    就是钉这个回归）。断言成功数恰好等于上限、其余 404、库里计数不超上限。
    """
    admin = _login(app_on)
    kb = _prepare_kb_with_doc(admin)
    limit, total = 3, 8
    token = _make_link(admin, kb, name="并发", max_visits=limit)["token"]

    barrier = threading.Barrier(total)
    lock = threading.Lock()
    codes: list[int] = []

    def fire():
        client = TestClient(app_on)         # 每线程独立客户端（各自跑 ASGI）
        barrier.wait()                      # 尽量同时发出，把并发窗口放到最大
        resp = client.post(f"/api/share/{token}/query",
                           json={"question": "住宿上限是多少"})
        with lock:
            codes.append(resp.status_code)

    threads = [threading.Thread(target=fire) for _ in range(total)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes.count(200) == limit, codes
    assert codes.count(404) == total - limit, codes
    assert _visit_count(app_on, token) == limit


def test_share_link_legacy_null_semantics(app_on):
    """T10 之前发出的链接（迁移补列后四列全 NULL）行为完全不变：不弹口令、
    不过期、不限次数——NULL 语义由读取端解释，且计次从 0 起正确累计。"""
    admin = _login(app_on)
    kb = _prepare_kb_with_doc(admin)
    token = _make_link(admin, kb, name="老链接")["token"]
    # 显式写回 NULL：等价于 T10 迁移给存量行补出来的状态
    with app_on.state.svc.sf() as s:
        s.execute(text(
            "UPDATE share_links SET expires_at=NULL, password_hash=NULL, "
            "max_visits=NULL, visit_count=NULL WHERE token=:t"), {"t": token})
        s.commit()

    anon = TestClient(app_on)
    for _ in range(3):
        assert anon.get(f"/api/share/{token}").status_code == 200
        events, _ = _stream_citations(anon, token, "住宿上限是多少")
        assert "done" in events
    assert _visit_count(app_on, token) == 3                 # NULL 起步也照常计次


def test_pre_t10_db_gets_share_link_columns(tmp_path, fake_embedder, monkeypatch):
    """T10 迁移：T10 之前的库 share_links 没有有效期/口令/次数四列。用只含旧
    列的库启动（create_all 只建缺失的表、不动既有表）→ 迁移补齐四列，存量
    链接行一律 NULL=不限，老链接照旧能匿名问答（行为与升级前一致）。

    与 T09 的 api_keys 补列测试同一套路；这里连读取端解释一起验——NULL 不是
    "空值报错"，而是"该维度不限"。
    """
    monkeypatch.setenv("KBASE_ADMIN_PASSWORD", "admin-pw")
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db = data_dir / "kbase.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE share_links (id VARCHAR(36) PRIMARY KEY, kb_id VARCHAR(36),
            kb_ids TEXT, token VARCHAR(64), name VARCHAR(200),
            provider VARCHAR(100), created_by VARCHAR(100), revoked BOOLEAN,
            created_at DATETIME);
    """)
    conn.execute(
        "INSERT INTO share_links (id, kb_id, kb_ids, token, name, provider,"
        " created_by, revoked, created_at) VALUES ('l1', 'kb-legacy', NULL,"
        " 'legacy-token-t10', '老链接', NULL, 'admin', 0, '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()

    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(data_dir).replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="on")

    # 迁移补齐四列，存量行四列全 NULL（没有 DEFAULT 回填这回事）
    with app.state.svc.sf() as s:
        cols = {c["name"] for c in inspect(s.get_bind()).get_columns("share_links")}
        assert {"expires_at", "password_hash", "max_visits", "visit_count"} <= cols
        assert tuple(s.execute(text(
            "SELECT expires_at, password_hash, max_visits, visit_count "
            "FROM share_links WHERE token='legacy-token-t10'")).one()) == (
                None, None, None, None)
        # 老链接绑定的库：T10 之前建的库（这里补一行，好把匿名问答跑通）
        s.add(KnowledgeBase(id="kb-legacy", name="老库"))
        s.commit()

    admin = _login(app)
    r = admin.post("/api/kb/kb-legacy/documents",
                   files=[("files", ("报销.md",
                                     "# 报销制度\n住宿上限每晚500元。".encode(),
                                     "text/markdown"))])
    assert r.status_code == 200, r.text

    anon = TestClient(app)
    assert anon.get("/api/share/legacy-token-t10").status_code == 200
    events, citations = _stream_citations(anon, "legacy-token-t10", "住宿上限是多少")
    assert "done" in events and citations                   # NULL=不限，照旧可用


def test_share_public_endpoints_rate_limited(app_on, monkeypatch):
    """T10 公开端点限流：三个端点注册在 app 级、吃不到 T09 挂在 /api router 上
    的限流依赖，必须手动调用同一份 limiter。token 一道闸 + 来源 IP 一道闸；
    三个端点共用一个桶（猛刷附图不能绕过问答限流）；超限 429 + Retry-After。"""
    monkeypatch.setattr(share_routes, "_SHARE_TOKEN_RPM", 2)
    monkeypatch.setattr(share_routes, "_SHARE_IP_RPM", 1000)
    admin = _login(app_on)
    kb = _prepare_kb_with_doc(admin)
    token = _make_link(admin, kb, name="限流")["token"]

    anon = TestClient(app_on, client=("10.9.9.9", 51234))
    assert anon.get(f"/api/share/{token}").status_code == 200
    assert anon.get(f"/api/share/{token}").status_code == 200
    r = anon.get(f"/api/share/{token}")                     # 第三次：token 闸
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "rate_limited"
    assert int(r.headers["retry-after"]) >= 1
    # 同桶：问答与附图也被同一道闸拦下（换端点不能绕）
    assert anon.get(f"/api/share/{token}/images/x/y.png").status_code == 429
    assert anon.post(f"/api/share/{token}/query",
                     json={"question": "x"}).status_code == 429
    # 换来源 IP 也绕不过 token 闸
    other_ip = TestClient(app_on, client=("10.9.9.10", 51234))
    assert other_ip.get(f"/api/share/{token}").status_code == 429


def test_share_public_endpoints_rate_limited_by_ip(app_on, monkeypatch):
    """T10 限流第二道闸：换 token 也绕不过来源 IP 上限（一个客户端刷遍所有
    拿到的链接照样被拦）。"""
    monkeypatch.setattr(share_routes, "_SHARE_TOKEN_RPM", 1000)
    monkeypatch.setattr(share_routes, "_SHARE_IP_RPM", 2)
    admin = _login(app_on)
    kb = _prepare_kb_with_doc(admin)
    t1 = _make_link(admin, kb, name="a")["token"]
    t2 = _make_link(admin, kb, name="b")["token"]

    anon = TestClient(app_on, client=("10.9.9.11", 51234))
    assert anon.get(f"/api/share/{t1}").status_code == 200
    assert anon.get(f"/api/share/{t2}").status_code == 200
    r = anon.get(f"/api/share/{t1}")                        # 第三次：IP 闸
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert anon.get(f"/api/share/{t2}").status_code == 429
