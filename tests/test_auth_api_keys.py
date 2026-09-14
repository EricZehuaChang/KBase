"""API Key 管理端点（admin）：POST 创建（一次性返回完整 key）/GET 列表
（不含 hash）/DELETE 吊销（软删除）。Bearer 通道校验已在 G2 的 deps.py
落地（见 test_auth_deps.py），这里补的是设置页 CRUD 与吊销后 401 的
端到端贯通（经由这几个新端点，而不是直接操作 DB）。

T09 追加：有效期/停用-恢复/IP 白名单/配额限流（429+Retry-After）/逐日用量/
流式事件序列不因限流而变/用量口径（上游真实值优先、拿不到才估算）。
限流的可控时钟靠注入 kbase.ratelimit.limiter.clock（模块级单例，用例间用
autouse fixture 清空）。
"""
import pytest
from fastapi.testclient import TestClient

from kbase import ratelimit
from tests.test_api import FakeLLM
from tests.test_auth import _client_on

KEY_IP = "10.1.2.3"          # 测试里假装的客户端来源 IP（TestClient(client=...)）


@pytest.fixture(autouse=True)
def _clean_limiter():
    """模块级限流单例跨用例共享，逐用例清空（含滑窗/当日计数/last_used 节流）。"""
    ratelimit.limiter.reset()
    yield
    ratelimit.limiter.reset()


class _Clock:
    """可控时钟（T09 测试）：now 可直接改，滑窗与"日"都读它。"""

    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _freeze(monkeypatch, now: float) -> _Clock:
    clock = _Clock(now)
    monkeypatch.setattr(ratelimit.limiter, "clock", clock)
    return clock


def _create_key(c, name="mcp-key", role="viewer", **extra):
    return c.post("/api/settings/api-keys", json={"name": name, "role": role, **extra})


def _login_admin(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    return app, c


def _key_client(app, ip=KEY_IP) -> TestClient:
    """只用 Bearer 的独立客户端：来源 IP 显式指定（IP 白名单要能测通/测断）。"""
    return TestClient(app, client=(ip, 51234))


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


# ---- T09：策略字段（有效期 / IP 白名单 / 配额 / 停用） -----------------------

def test_create_api_key_accepts_policy_fields(tmp_path, fake_embedder, monkeypatch):
    """创建即可带 T09 的全部策略字段；列表原样回读，且仍不出现任何密钥材料。"""
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    r = _create_key(c, name="limited", rpm=30, daily_quota=500,
                    expires_at="2030-01-31",
                    ip_allow=["10.1.2.3", "192.168.0.0/16"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rpm"] == 30 and body["daily_quota"] == 500
    assert body["ip_allow"] == ["10.1.2.3", "192.168.0.0/16"]
    assert body["disabled"] is False
    # 纯日期按当日 23:59:59 处理（选"今天"不应立刻过期）
    assert body["expires_at"].startswith("2030-01-31T23:59:59")

    item = c.get("/api/settings/api-keys").json()[0]
    assert item["rpm"] == 30 and item["daily_quota"] == 500
    assert item["ip_allow"] == ["10.1.2.3", "192.168.0.0/16"]
    assert item["last_used_at"] is None
    assert "key" not in item and "key_hash" not in item


def test_create_api_key_rejects_bad_ip_and_over_limit(tmp_path, fake_embedder, monkeypatch):
    """IP 白名单在写入侧就拦住非法值/超量——鉴权侧对脏数据 fail closed，
    放进库里等于把这把 Key 锁死（见 kbase/ratelimit.py 的 ip_allowed）。"""
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    assert _create_key(c, ip_allow=["not-an-ip"]).status_code == 422
    assert _create_key(c, ip_allow=[f"10.0.0.{i}" for i in range(21)]).status_code == 422
    assert _create_key(c, rpm=0).status_code == 422      # 0 无意义：停用请用 disabled


def test_expired_api_key_401_with_code(tmp_path, fake_embedder, monkeypatch):
    """过期：401 且 detail.code=api_key_expired（与"凭据无效"区分开，
    集成方据此知道该换 Key 而不是查网络）。"""
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    created = _create_key(c, name="short-lived").json()
    r = c.patch(f"/api/settings/api-keys/{created['id']}",
                json={"expires_at": "2020-01-01T00:00:00"})
    assert r.status_code == 200, r.text

    anon = _key_client(app)
    resp = anon.get("/api/kb", headers={"Authorization": f"Bearer {created['key']}"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "api_key_expired"

    # 延期回未来 → 恢复可用（延期是 PATCH 的正当用途）
    c.patch(f"/api/settings/api-keys/{created['id']}",
            json={"expires_at": "2035-01-01"})
    assert anon.get("/api/kb",
                    headers={"Authorization": f"Bearer {created['key']}"}).status_code == 200


def test_disable_then_reenable(tmp_path, fake_embedder, monkeypatch):
    """disabled 与 revoked 是两回事：停用可恢复（PATCH），吊销不可恢复（DELETE）。
    停用期间 401（普通未认证语义，不额外给 code）。"""
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    created = _create_key(c, name="toggling").json()
    anon = _key_client(app)
    hdr = {"Authorization": f"Bearer {created['key']}"}
    assert anon.get("/api/kb", headers=hdr).status_code == 200

    r = c.patch(f"/api/settings/api-keys/{created['id']}", json={"disabled": True})
    assert r.status_code == 200 and r.json()["disabled"] is True
    # PATCH 的响应体也是同一套投影：不含 key_hash/完整 key
    assert "key" not in r.json() and "key_hash" not in r.json()
    off = anon.get("/api/kb", headers=hdr)
    assert off.status_code == 401
    assert "code" not in str(off.json().get("detail"))   # 不泄漏"只是被停用"
    assert c.get("/api/settings/api-keys").json()[0]["revoked"] is False

    c.patch(f"/api/settings/api-keys/{created['id']}", json={"disabled": False})
    assert anon.get("/api/kb", headers=hdr).status_code == 200


def test_ip_allow_exact_and_cidr(tmp_path, fake_embedder, monkeypatch):
    """来源 IP 白名单：精确 IP 命中放行；CIDR 命中放行；不在白名单 403
    ip_not_allowed。来源取对端地址（不信任 X-Forwarded-For），故用
    TestClient(client=...) 造来源 IP；伪造 XFF 也不能绕过。"""
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    exact = _create_key(c, name="exact", ip_allow=[KEY_IP]).json()["key"]
    cidr = _create_key(c, name="cidr", ip_allow=["10.0.0.0/8"]).json()["key"]
    other = _create_key(c, name="other", ip_allow=["192.168.0.0/16"]).json()["key"]

    from_ip = _key_client(app, ip=KEY_IP)
    assert from_ip.get("/api/kb", headers={"Authorization": f"Bearer {exact}"}).status_code == 200
    assert from_ip.get("/api/kb", headers={"Authorization": f"Bearer {cidr}"}).status_code == 200
    denied = from_ip.get("/api/kb", headers={"Authorization": f"Bearer {other}"})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "ip_not_allowed"

    # 伪造 XFF 不生效：来源仍按对端地址判定
    spoofed = from_ip.get("/api/kb", headers={
        "Authorization": f"Bearer {other}", "X-Forwarded-For": KEY_IP})
    assert spoofed.status_code == 403
    # 反向同理：对端不在白名单时，XFF 说自己在白名单里也没用
    outside = _key_client(app, ip="172.16.0.9")
    assert outside.get("/api/kb", headers={
        "Authorization": f"Bearer {exact}", "X-Forwarded-For": KEY_IP}).status_code == 403
    # 白名单=NULL/未配置 → 不限来源（老库/未设置行为不变）
    open_key = _create_key(c, name="open").json()["key"]
    assert outside.get("/api/kb",
                       headers={"Authorization": f"Bearer {open_key}"}).status_code == 200


def test_rpm_exceeded_429_with_retry_after(tmp_path, fake_embedder, monkeypatch):
    """rpm 用进程内 60 秒滑窗：第 rpm+1 次在窗口内 → 429 + Retry-After
    （剩余等待秒数，整数、≥1）；等窗口滑过 → 恢复放行。"""
    clock = _freeze(monkeypatch, 1_800_000_000.0)
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    key = _create_key(c, name="burst", rpm=3).json()["key"]
    anon = _key_client(app)
    hdr = {"Authorization": f"Bearer {key}"}

    for _ in range(3):
        assert anon.get("/api/kb", headers=hdr).status_code == 200
    over = anon.get("/api/kb", headers=hdr)
    assert over.status_code == 429
    retry_after = int(over.headers["Retry-After"])
    assert 1 <= retry_after <= 60
    assert over.json()["detail"]["code"] == "rate_limited"

    # 拒绝的请求不推后窗口：立刻再打一次，等待时间只会缩短不会变长
    again = anon.get("/api/kb", headers=hdr)
    assert int(again.headers["Retry-After"]) <= retry_after

    clock.now += retry_after          # 窗口滑过 → 放行
    assert anon.get("/api/kb", headers=hdr).status_code == 200


def test_daily_quota_resets_across_days(tmp_path, fake_embedder, monkeypatch):
    """daily_quota 按 UTC 日计数：当日超限 429（Retry-After=到次日 0 点），
    跨日后计数归零重新放行。"""
    clock = _freeze(monkeypatch, 1_800_000_000.0)   # 2027-01-15 08:00:00 UTC
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    key = _create_key(c, name="daily", daily_quota=2).json()["key"]
    anon = _key_client(app)
    hdr = {"Authorization": f"Bearer {key}"}

    assert anon.get("/api/kb", headers=hdr).status_code == 200
    assert anon.get("/api/kb", headers=hdr).status_code == 200
    blocked = anon.get("/api/kb", headers=hdr)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) == 16 * 3600   # 距次日 0 点整 16h

    clock.now += 16 * 3600            # 跨到 UTC 次日 00:00
    assert anon.get("/api/kb", headers=hdr).status_code == 200


def test_last_used_at_write_is_throttled_per_minute(tmp_path, fake_embedder, monkeypatch):
    """last_used_at 写库节流到每 Key 每分钟至多一次（鉴权在每请求的关键路径
    上，不能每个请求都写库）。节流判定读注入时钟，故用推进时钟验证"写/不写"；
    写入值本身取真实时间（datetime.utcnow），所以断言"变了/没变"。"""
    clock = _freeze(monkeypatch, 1_800_000_000.0)
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    created = _create_key(c, name="throttled").json()
    anon = _key_client(app)
    hdr = {"Authorization": f"Bearer {created['key']}"}

    def last_used():
        return c.get("/api/settings/api-keys").json()[0]["last_used_at"]

    assert last_used() is None                     # NULL=从未使用
    assert anon.get("/api/kb", headers=hdr).status_code == 200
    first = last_used()
    assert first is not None                       # 首次使用即落库

    clock.now += 30                                # 一分钟内 → 不再写
    assert anon.get("/api/kb", headers=hdr).status_code == 200
    assert last_used() == first

    clock.now += 31                                # 超过一分钟 → 再写一次
    assert anon.get("/api/kb", headers=hdr).status_code == 200
    assert last_used() != first


def test_usage_accumulates_and_is_admin_only(tmp_path, fake_embedder, monkeypatch):
    """逐日用量累计：每个放行的 Bearer 请求计 1（见 kbase/ratelimit.py 的
    record_requests）。用量只经管理端鉴权接口暴露——无鉴权的 /metrics 里
    不能出现任何 per-key 数据。"""
    clock = _freeze(monkeypatch, 1_800_000_000.0)
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    created = _create_key(c, name="metered", rpm=100).json()
    anon = _key_client(app)
    hdr = {"Authorization": f"Bearer {created['key']}"}
    for _ in range(4):
        assert anon.get("/api/kb", headers=hdr).status_code == 200
    assert anon.get("/api/kb", headers=hdr).status_code == 200

    usage = c.get(f"/api/settings/api-keys/{created['id']}/usage?days=30")
    assert usage.status_code == 200, usage.text
    body = usage.json()
    assert body["key_id"] == created["id"]
    assert body["totals"]["requests"] == 5
    assert [i["day"] for i in body["items"]] == [ratelimit.utc_day(clock.now)]
    assert body["items"][0]["requests"] == 5

    # 非管理员看不到用量（鉴权面：用量是管理数据）
    weak = _create_key(c, name="viewer-key", role="viewer").json()["key"]
    forbidden = anon.get(f"/api/settings/api-keys/{created['id']}/usage",
                         headers={"Authorization": f"Bearer {weak}"})
    assert forbidden.status_code == 403

    # /metrics 无鉴权：绝不能带 per-key 维度（Key id / 名字都不出现在出口里）
    metrics = c.get("/metrics").text
    assert created["id"] not in metrics
    assert "metered" not in metrics

    # 未知 Key 的用量 → 404
    assert c.get("/api/settings/api-keys/nope/usage").status_code == 404


# ---- T09：限流不得改变流式事件序列 ------------------------------------------

def _kb_with_doc(c) -> str:
    kb = c.post("/api/kb", json={"name": "制度库"}).json()["id"]
    c.post(f"/api/kb/{kb}/documents",
           files=[("files", ("补贴.md", "# 补贴\n住房补贴入职满两年可申领。".encode("utf-8"),
                             "text/markdown"))])
    return kb


def _api_events(c, kb: str, key: str) -> list[tuple[str, str]]:
    """收集 /api/kb/{id}/query 的 SSE 事件序列（event 名 + data）。
    c 必须是**只带 Bearer** 的客户端——带管理端 Cookie 的客户端会走 Cookie
    通道，测不到 API Key 这条路径。"""
    events: list[tuple[str, str]] = []
    with c.stream("POST", f"/api/kb/{kb}/query",
                  json={"question": "住房补贴怎么申领？"},
                  headers={"Authorization": f"Bearer {key}"}) as r:
        assert r.status_code == 200, r.status_code
        event = ""
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                events.append((event, line[5:].strip()))
    return events


def _v1_events(c, kb: str, key: str) -> tuple[str | None, list[str]]:
    """收集 /v1/chat/completions(stream=true) 的 data 行序列与估算标记头。"""
    chunks: list[str] = []
    with c.stream("POST", "/v1/chat/completions",
                  json={"model": kb, "stream": True,
                        "messages": [{"role": "user", "content": "住房补贴怎么申领？"}]},
                  headers={"Authorization": f"Bearer {key}"}) as r:
        assert r.status_code == 200, r.status_code
        marker = r.headers.get("x-kbase-usage-estimated")
        for line in r.iter_lines():
            if line.startswith("data:"):
                data = line[5:].strip()
                chunks.append(data)
                if data == "[DONE]":
                    break
    return marker, chunks


def _v1_shape(chunks: list[str]) -> list[str]:
    """把 /v1 流的 data 行归一成"事件形状"序列——chunk 里的 id/created 每次
    请求都不同，比对形状才能验证"事件序列未变"（首块 role → token* →
    末块 finish=stop → [DONE]）。"""
    import json as _json
    shape: list[str] = []
    for data in chunks:
        if data == "[DONE]":
            shape.append("done")
            continue
        choice = _json.loads(data)["choices"][0]
        if choice["finish_reason"] == "stop":
            shape.append("final")
        elif "role" in choice["delta"]:
            shape.append("role")
        else:
            shape.append("token")
    return shape


def test_stream_event_sequence_unchanged_when_rate_limit_passes(
        tmp_path, fake_embedder, monkeypatch):
    """限流放行时两条流式端点的事件序列必须与"无限流"完全一致——限流是前置
    闸门（超限直接 429 JSON），绝不能往流里插事件。同时校验 /v1 的 usage
    估算与 x-kbase-usage-estimated 标记。"""
    _freeze(monkeypatch, 1_800_000_000.0)
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    kb = _kb_with_doc(c)
    plain = _create_key(c, name="no-limits").json()["key"]
    limited = _create_key(c, name="limited", rpm=100, daily_quota=1000).json()["key"]
    anon = TestClient(app)          # 只带 Bearer，不带管理端 Cookie

    api_plain = _api_events(anon, kb, plain)
    api_limited = _api_events(anon, kb, limited)
    assert [e for e, _ in api_plain] == ["citations", "token", "token", "done"]
    assert api_limited == api_plain

    v1_plain_marker, v1_plain = _v1_events(anon, kb, plain)
    v1_limited_marker, v1_limited = _v1_events(anon, kb, limited)
    assert _v1_shape(v1_plain) == ["role", "token", "token", "final", "done"]
    assert _v1_shape(v1_limited) == _v1_shape(v1_plain)
    # 口径标注：测试用的 FakeLLM 不实现 usage_sink（不回调真实用量），故这里
    # 必然走字符估算 → 末块 usage_estimated 为 True。**流式的口径标记以末块
    # payload 为准**（SSE 响应头在生成结束前就已发出，无法事后改判）。
    # 真实用量路径另有专测 test_v1_usage_prefers_real_upstream_usage。
    import json as _json
    for chunks in (v1_plain, v1_limited):
        assert _json.loads(chunks[-2])["usage_estimated"] is True

    import json as _json
    last = _json.loads(v1_plain[-2])          # 末块（finish=stop）带引用与用量
    assert last["choices"][0]["finish_reason"] == "stop"
    assert last["citations"]
    assert last["usage"]["prompt_tokens"] > 0
    assert last["usage"]["total_tokens"] == (last["usage"]["prompt_tokens"]
                                            + last["usage"]["completion_tokens"])


def test_v1_reports_usage_and_meters_per_key(tmp_path, fake_embedder, monkeypatch):
    """非流式 /v1：usage 不再是 0 占位，而是按字符数的估算值（带
    x-kbase-usage-estimated 头），并按 Key 落到逐日用量里。"""
    _freeze(monkeypatch, 1_800_000_000.0)
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    kb = _kb_with_doc(c)
    created = _create_key(c, name="v1-key").json()
    anon = _key_client(app)
    hdr = {"Authorization": f"Bearer {created['key']}"}
    r = anon.post("/v1/chat/completions",
                  json={"model": kb,
                        "messages": [{"role": "user", "content": "住房补贴怎么申领？"}]},
                  headers=hdr)
    assert r.status_code == 200, r.text
    usage = r.json()["usage"]
    assert r.headers["x-kbase-usage-estimated"] == "1"
    assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

    body = c.get(f"/api/settings/api-keys/{created['id']}/usage").json()
    assert body["totals"]["requests"] == 1
    assert body["totals"]["prompt_tokens"] == usage["prompt_tokens"]
    assert body["totals"]["completion_tokens"] == usage["completion_tokens"]
    assert body["tokens_estimated"] is True


def test_v1_usage_prefers_real_upstream_usage(tmp_path, fake_embedder,
                                              monkeypatch):
    """T09 计量口径：上游透出真实 usage 时必须记真实值，而不是字符估算。

    用一个"会回真实用量"的 LLM 打桩（模拟 OpenAI 兼容端点经
    stream_options.include_usage 在末块给出 usage）：
    - 响应的 usage 必须是上游那份（10/20/30），不是估算值；
    - 估算标记头 x-kbase-usage-estimated 必须**不出现**（真实值无需标注）；
    - 落库的 usage 行 tokens_estimated 必须为 False。
    """
    _freeze(monkeypatch, 1_800_000_000.0)
    app, c = _login_admin(tmp_path, fake_embedder, monkeypatch)
    kb = _kb_with_doc(c)
    key = _create_key(c, name="metered").json()["key"]

    # 在应用装配后替换 provider 的 stream：只对 usage_sink 回调真实值
    svc = app.state.svc
    real = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}

    class UsageLLM:
        model = "usage-fake"

        async def stream(self, messages, usage_sink=None, **params):
            if usage_sink is not None:
                usage_sink(dict(real))
            yield "答案"

        async def complete(self, messages, usage_sink=None, **params):
            if usage_sink is not None:
                usage_sink(dict(real))
            return "答案"

    monkeypatch.setattr(svc, "get_llm", lambda provider=None: UsageLLM())

    anon = TestClient(app)
    _marker, chunks = _v1_events(anon, kb, key)

    import json as _json
    last = _json.loads(chunks[-2])
    assert last["usage"] == real, last["usage"]
    assert last["usage_estimated"] is False, "真实用量不得标为估算"

    # 落库口径：tokens_estimated 必须是 False（真实值）
    import sqlite3
    from kbase.models import ApiKey
    with app.state.svc.sf() as s:
        row = s.query(ApiKey).filter_by(name="metered").one()
        key_id = row.id
    db = sqlite3.connect(str(tmp_path / "data" / "kbase.sqlite"))
    est = db.execute(
        "SELECT tokens_estimated, prompt_tokens, completion_tokens "
        "FROM api_key_usage_daily WHERE key_id=?", (key_id,)).fetchone()
    assert est is not None, "应有当天用量行"
    assert est[0] in (0, False), f"真实用量不应标为估算：{est}"
    assert (est[1], est[2]) == (10, 20)

    # 非流式同一口径：真实用量 → 报真实值、usage_estimated=false、
    # **不发** x-kbase-usage-estimated 估算头（非流式在响应发出前就已知口径，
    # 头是流式给不了才退而用 payload 字段，见 routes/openai_compat.py 注释）
    r = anon.post("/v1/chat/completions",
                  json={"model": kb,
                        "messages": [{"role": "user", "content": "住房补贴怎么申领？"}]},
                  headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    assert r.json()["usage"] == real
    assert r.json()["usage_estimated"] is False
    assert "x-kbase-usage-estimated" not in r.headers

    # 反向对照：拿不到真实用量的 provider（FakeLLM）→ 估算 + 非流式带估算头
    monkeypatch.setattr(svc, "get_llm", lambda provider=None: FakeLLM())
    r2 = anon.post("/v1/chat/completions",
                   json={"model": kb,
                         "messages": [{"role": "user", "content": "住房补贴怎么申领？"}]},
                   headers={"Authorization": f"Bearer {key}"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["usage_estimated"] is True
    assert r2.headers["x-kbase-usage-estimated"] == "1"
