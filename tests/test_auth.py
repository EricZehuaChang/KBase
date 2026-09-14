"""端到端鉴权测试（auth="on"）：登录/登出/me、Cookie 会话贯通、无凭据 401、
Origin 不匹配 403、豁免路径可达、bootstrap 首启 admin。复用 tests/test_api.py
的 CFG/FakeLLM，但每个 create_app 调用都显式 auth="on"（默认值，写出来更明确）。

T11 追加：登录/口令端点的人机闸（连续失败→429+Retry-After、窗口到点解锁、
用户名与 IP 两个维度独立、成功登录不清别人的计数、锁定响应与 401 逐字节一致）。
"""
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from kbase import ratelimit
from kbase.api.main import create_app
from kbase.auth import security
from kbase.models import AuditLog, User
from tests.test_api import CFG, FakeLLM


def _client_on(tmp_path, fake_embedder, *, admin_password=None, monkeypatch=None):
    if admin_password is not None:
        monkeypatch.setenv("KBASE_ADMIN_PASSWORD", admin_password)
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="on")
    return app, TestClient(app)


def test_login_ok_sets_cookie_and_returns_role(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    assert r.status_code == 200
    assert r.json() == {"username": "admin", "role": "superadmin"}
    assert "kbase_session" in r.cookies
    # 会话级 Cookie：不带 Max-Age/Expires（关浏览器即清除，重开须重新登录）
    set_cookie = r.headers["set-cookie"]
    assert "kbase_session" in set_cookie
    assert "Max-Age" not in set_cookie and "Expires" not in set_cookie

    # 勾选"记住登录"：持久 Cookie，时长与 JWT 有效期对齐（30 天）
    r = c.post("/api/auth/login", json={"username": "admin",
                                        "password": "adminpass123",
                                        "remember": True})
    assert r.status_code == 200
    from kbase.auth.security import SESSION_TOKEN_TTL_SECONDS
    assert f"Max-Age={SESSION_TOKEN_TTL_SECONDS}" in r.headers["set-cookie"]


def test_login_bad_password_401(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    r = c.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_login_disabled_user_401(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    from kbase.db import make_session_factory
    data_dir = tmp_path / "data"
    sf = make_session_factory(f"sqlite:///{data_dir}/kbase.sqlite")
    with sf() as s:
        s.add(User(id="u-viewer", username="viewer1",
                   password_hash=security.hash_password("pw12345"),
                   role="viewer", disabled=True))
        s.commit()
    r = c.post("/api/auth/login", json={"username": "viewer1", "password": "pw12345"})
    assert r.status_code == 401


def test_no_credentials_401_on_sample_route(tmp_path, fake_embedder):
    app, c = _client_on(tmp_path, fake_embedder)
    r = c.get("/api/kb")
    assert r.status_code == 401


def test_cookie_session_end_to_end(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    login = c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    assert login.status_code == 200
    r = c.get("/api/kb")
    assert r.status_code == 200
    assert r.json() == []


def test_logout_then_401(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    assert c.get("/api/kb").status_code == 200
    logout = c.post("/api/auth/logout")
    assert logout.status_code == 200
    assert c.get("/api/kb").status_code == 401


def test_auth_me_returns_username_and_role(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    r = c.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json() == {"username": "admin", "role": "superadmin", "email": None,
                        "advanced_ui": True, "language": None,
                        "tour_enabled": True}   # 超管恒可见导览入口


def test_origin_mismatch_403_on_mutating_request(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    r = c.post("/api/kb", json={"name": "x"},
               headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403


def test_login_reachable_without_credentials(tmp_path, fake_embedder, monkeypatch):
    """POST /api/auth/login 本身豁免鉴权（否则无法登录）：错误凭据应拿到 401
    而不是 401（未认证）以外的语义混淆——这里只验证该端点不因缺 Cookie/Key
    而被全局 actor 依赖拦截（不是 401 unauthenticated 的 WWW-Authenticate 形式）。"""
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    r = c.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 401
    assert "WWW-Authenticate" not in r.headers    # 是登录失败 401，不是鉴权豁免拦截的 401


def test_healthz_and_spa_reachable_without_auth(tmp_path, fake_embedder):
    app, c = _client_on(tmp_path, fake_embedder)
    assert c.get("/healthz").status_code == 200
    r = c.get("/kb")     # SPA 深链接回退
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ---- M5-1 F1：双 SPA 回退（SPAStaticFiles，/admin 前缀回退到管理端 bundle）
# 三个测试用 web/index.html 与 web/admin.html 各自的 <title> 作为 marker
# 区分命中了哪个产物（两份 HTML 的 <title> 分别是 "KBase" 与
# "KBase 管理端"，见 web-app/index.html、web-app/admin.html 的注释）——这些
# 断言依赖仓库里已提交的构建产物 web/（house rule：构建产物入库），与既有
# test_spa_deep_link_serves_index / test_healthz_and_spa_reachable_without_auth
# 的做法一致。

def test_admin_route_serves_admin_html_marker(tmp_path, fake_embedder):
    app, c = _client_on(tmp_path, fake_embedder)
    r = c.get("/admin")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "KBase 管理端" in r.text


def test_root_route_serves_index_html_marker(tmp_path, fake_embedder):
    """GET / 应回退使用端 index.html，不能被 /admin 分流逻辑误伤——两份产物
    的 marker 互斥断言，防止"两个都回退成同一份"这种更隐蔽的坏法。"""
    app, c = _client_on(tmp_path, fake_embedder)
    r = c.get("/")
    assert r.status_code == 200
    assert "<title>KBase</title>" in r.text
    assert "KBase 管理端" not in r.text


def test_admin_deep_link_falls_back_to_admin_html(tmp_path, fake_embedder):
    """管理端前端路由深链接（如 /admin/kb 刷新页面）未命中真实文件时应回退
    admin.html 而不是 index.html——否则深链接刷新会加载错误的 bundle，
    管理端路由（vue-router base="/admin"）拿到的却是使用端的 JS。"""
    app, c = _client_on(tmp_path, fake_embedder)
    r = c.get("/admin/users-page-route")
    assert r.status_code == 200
    assert "KBase 管理端" in r.text


def test_api_docs_disabled_when_auth_on_enabled_when_off(tmp_path, fake_embedder):
    """生产（auth="on"）关闭 /docs /redoc /openapi.json——它们默认不鉴权，
    会把完整路由与模型 schema 暴露给未认证访问者；dev/test（auth="off"）保留。"""
    app_on, c_on = _client_on(tmp_path, fake_embedder)
    assert c_on.get("/openapi.json").status_code == 404

    off_dir = tmp_path / "off"
    off_dir.mkdir()
    cfg = off_dir / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(off_dir / "data").replace("\\", "/")),
                   encoding="utf-8")
    app_off = create_app(config_path=cfg, embedder=fake_embedder,
                         llms={"fake": FakeLLM()}, reranker=False, auth="off")
    c_off = TestClient(app_off)
    assert c_off.get("/openapi.json").status_code == 200


def test_bootstrap_admin_created_on_startup(tmp_path, fake_embedder):
    app, c = _client_on(tmp_path, fake_embedder)
    from kbase.db import make_session_factory
    sf = make_session_factory(f"sqlite:///{tmp_path}/data/kbase.sqlite")
    with sf() as s:
        users = s.query(User).all()
        assert len(users) == 1
        assert users[0].username == "admin"
        assert users[0].role == "superadmin"   # 引导账号=超管（最高层级）


def test_bootstrap_env_password_honored(tmp_path, fake_embedder, monkeypatch):
    app, c = _client_on(tmp_path, fake_embedder, admin_password="explicit-pw-1",
                        monkeypatch=monkeypatch)
    r = c.post("/api/auth/login", json={"username": "admin", "password": "explicit-pw-1"})
    assert r.status_code == 200


def test_bootstrap_idempotent_across_app_restarts(tmp_path, fake_embedder, monkeypatch, caplog):
    """同一 data_dir 下重新 create_app（模拟重启）不应重复创建 admin 或再生成
    新的随机密码——第二次启动时 users 表已非空，ensure_admin 应跳过。"""
    monkeypatch.delenv("KBASE_ADMIN_PASSWORD", raising=False)
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    create_app(config_path=cfg, embedder=fake_embedder,
              llms={"fake": FakeLLM()}, reranker=False, auth="on")
    caplog.clear()    # 丢弃第一次启动产生的记录，只看第二次（"重启"）是否还打日志
    with caplog.at_level(logging.WARNING):
        create_app(config_path=cfg, embedder=fake_embedder,
                  llms={"fake": FakeLLM()}, reranker=False, auth="on")
    # 第二次启动不应再打随机密码日志（已引导过，ensure_admin 提前 return）
    assert not any("首启引导" in rec.message for rec in caplog.records)
    from kbase.db import make_session_factory
    sf = make_session_factory(f"sqlite:///{tmp_path}/data/kbase.sqlite")
    with sf() as s:
        assert s.query(User).count() == 1


# ---- T11：登录/口令端点的人机闸（审计表计数 + 指数退避） ----
#
# 可控时钟用"真实当下 + 偏移"，不能像 T09 那样冻结成任意绝对值：登录闸数的
# 是 audit_logs 里的行，行的 ts 由 write_audit 用 datetime.utcnow() 落库（真实
# 墙上时间），把时钟冻到过去/未来会让窗口边界与库里的行对不上。偏移只表达
# "又过了多久"，于是不 sleep 也能验证窗口滑过。
#
# 默认闸参数（kbase/config.py 的 LoginGuardConfig，config/kbase.yaml 有同款
# 注释）：5 次 / 15 分钟；退避 30s 起、每挨一次拒绝翻一倍、上限 900s。


class _OffsetClock:
    """真实当下 + 可调偏移（秒）的可控时钟。"""

    def __init__(self):
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


@pytest.fixture
def guard_clock(monkeypatch):
    """换掉登录闸的时钟（模块级单例，默认 time.time）。"""
    clock = _OffsetClock()
    monkeypatch.setattr(ratelimit.login_guard, "clock", clock)
    return clock


def _sf(tmp_path):
    """按用例的 data_dir 单开一个会话工厂（在 app 之外读同一份 sqlite）。"""
    from kbase.db import make_session_factory
    return make_session_factory(f"sqlite:///{tmp_path}/data/kbase.sqlite")


def _audit_rows(tmp_path, action) -> list:
    sf = _sf(tmp_path)
    with sf() as s:
        return s.query(AuditLog).filter_by(action=action).all()


def _lock_details(tmp_path, action=ratelimit.LOGIN_LOCKED) -> list:
    """闸拦下请求时留下的审计行 detail 列表（按时间升序）。"""
    return [json.loads(row.detail)
            for row in sorted(_audit_rows(tmp_path, action), key=lambda r: r.ts)]


def _last_lock_detail(tmp_path) -> dict:
    """最近一行 login_locked 审计的 detail（闸自己留下的判定依据）。"""
    details = _lock_details(tmp_path)
    assert details, "没有被闸拦下的审计行（action=login_locked）"
    return details[-1]


def _fail_login(c, username, times=1, password="wrong"):
    """连续失败 times 次，返回最后一次响应（默认参数下 5 次以内都该是 401）。"""
    r = None
    for _ in range(times):
        r = c.post("/api/auth/login",
                   json={"username": username, "password": password})
    return r


def _add_user(tmp_path, username, password, role="viewer"):
    sf = _sf(tmp_path)
    with sf() as s:
        s.add(User(id=f"u-{username}", username=username, role=role,
                   password_hash=security.hash_password(password)))
        s.commit()


def test_login_locked_after_consecutive_failures(tmp_path, fake_embedder,
                                                 monkeypatch):
    """连续 5 次失败后第 6 次进闸：429 + Retry-After，且**正确密码也进不来**
    （闸在鉴权之前，锁定与"密码对不对"无关）。"""
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    bad = c.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert bad.status_code == 401
    assert "Retry-After" not in bad.headers      # 未锁定时不带这个头
    assert _fail_login(c, "admin", 4).status_code == 401   # 累计 5 次失败=达阈值

    locked = c.post("/api/auth/login", json={"username": "admin",
                                             "password": "adminpass123"})
    assert locked.status_code == 429
    assert locked.headers["Retry-After"] == "30"      # 退避基数，尚无被拒档位
    # 锁定响应与"输错密码"的 401 正文**逐字节一致**，唯一差别是状态码与
    # Retry-After 头（锁定本身是可观测的，账号是否存在不可观测）
    assert locked.content == bad.content
    assert "WWW-Authenticate" not in locked.headers

    # 锁事件落审计行：detail 里带两个维度的窗口内计数与报出的退避时长
    detail = _last_lock_detail(tmp_path)
    assert detail["endpoint"] == "login"
    assert detail["username_attempts"] == 5 and detail["ip_attempts"] == 5
    assert detail["retry_after"] == 30 and detail["username_refusals"] == 0
    # 被闸拦下的请求不写 login_failed（它压根没被验证过，写进去是假账）
    assert len(_audit_rows(tmp_path, ratelimit.LOGIN_FAILED)) == 5


def test_login_lock_released_after_window_expires(tmp_path, fake_embedder,
                                                  monkeypatch, guard_clock):
    """窗口（默认 15 分钟）整体滑过后计数归零、锁自动解开——不需要任何清理
    动作；窗口没走完时即使密码正确也依然被拦。"""
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    assert _fail_login(c, "admin", 5).status_code == 401
    assert c.post("/api/auth/login", json={"username": "admin",
                                           "password": "adminpass123"}
                  ).status_code == 429
    guard_clock.offset = 880               # 窗口还差 20 秒走完 → 仍然锁着
    assert c.post("/api/auth/login", json={"username": "admin",
                                           "password": "adminpass123"}
                  ).status_code == 429
    guard_clock.offset = 905               # 失败行整体滑出窗口（留几秒余量）
    ok = c.post("/api/auth/login", json={"username": "admin",
                                         "password": "adminpass123"})
    assert ok.status_code == 200
    assert "kbase_session" in ok.cookies
    # 闸是整体复位（不是"正确密码有特权"）：错的密码重新按 401 计
    assert c.post("/api/auth/login", json={"username": "admin",
                                           "password": "wrong"}).status_code == 401


def test_login_backoff_grows_exponentially_with_cap(tmp_path, fake_embedder,
                                                    monkeypatch):
    """锁定期间继续硬打：退避按"失败档位 + 已被拒次数"翻倍，直到上限 900s
    （默认与窗口同长——报得比窗口更久只是虚报）。"""
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    assert _fail_login(c, "admin", 5).status_code == 401
    waits = []
    for _ in range(7):
        r = c.post("/api/auth/login", json={"username": "admin",
                                            "password": "adminpass123"})
        assert r.status_code == 429
        waits.append(int(r.headers["Retry-After"]))
    # 30·2^0 … 30·2^4 后封顶：480 的下一档 960 被 cap 到 900，之后恒定
    assert waits == [30, 60, 120, 240, 480, 900, 900]


def test_success_does_not_clear_another_users_counter(tmp_path, fake_embedder,
                                                      monkeypatch):
    """一个用户登录成功不得清掉**别人**的失败计数（计数按用户名/IP 分别计时，
    与"谁刚登录成功"无关）。"""
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    _add_user(tmp_path, "viewer1", "pw123456")
    assert _fail_login(c, "admin", 4).status_code == 401
    # 另一个账号用正确密码登录成功——不该顺带把 admin 的计数清零
    assert c.post("/api/auth/login", json={"username": "viewer1",
                                           "password": "pw123456"}
                  ).status_code == 200
    assert _fail_login(c, "admin", 1).status_code == 401     # 第 5 次失败照记
    locked = c.post("/api/auth/login", json={"username": "admin",
                                             "password": "adminpass123"})
    assert locked.status_code == 429
    detail = _last_lock_detail(tmp_path)
    assert detail["username_attempts"] == 5                  # admin 的计数没被清
    # 审计里只有 admin 的 5 条失败；viewer1 那次成功记的是 login_success
    assert len(_audit_rows(tmp_path, ratelimit.LOGIN_FAILED)) == 5
    assert len(_audit_rows(tmp_path, "login_success")) == 1


def test_username_rule_locks_across_ips(tmp_path, fake_embedder, monkeypatch):
    """用户名维度独立生效：账号被爆 5 次后，换一个干净 IP 用**正确密码**也
    进不来；同一时刻别的用户名不受影响。"""
    app, _ = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    attacker = TestClient(app, client=("10.0.0.1", 40001))
    assert _fail_login(attacker, "admin", 5).status_code == 401

    clean_ip = TestClient(app, client=("10.0.0.2", 40002))
    locked = clean_ip.post("/api/auth/login", json={"username": "admin",
                                                    "password": "adminpass123"})
    assert locked.status_code == 429
    detail = _last_lock_detail(tmp_path)
    assert detail["username_attempts"] == 5 and detail["ip_attempts"] == 0
    # 同一个干净 IP 上的其他用户名照常走（401=确实没这个账号，不是被闸拦）
    assert clean_ip.post("/api/auth/login", json={"username": "no.such.user",
                                                  "password": "x"}
                         ).status_code == 401


def test_ip_rule_locks_other_usernames(tmp_path, fake_embedder, monkeypatch):
    """IP 维度独立生效：同一 IP 用 5 个不同用户名试错后，该 IP 上**任何**
    用户名的登录都被拦下（此时 admin 自己的失败计数是 0）；换个 IP 就正常。"""
    app, _ = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    attacker = TestClient(app, client=("10.0.0.3", 40003))
    for i in range(5):
        assert attacker.post("/api/auth/login",
                             json={"username": f"guess{i}", "password": "x"}
                             ).status_code == 401

    locked = attacker.post("/api/auth/login", json={"username": "admin",
                                                    "password": "adminpass123"})
    assert locked.status_code == 429
    detail = _last_lock_detail(tmp_path)
    assert detail["ip_attempts"] == 5 and detail["username_attempts"] == 0

    other_ip = TestClient(app, client=("10.0.0.4", 40004))
    assert other_ip.post("/api/auth/login", json={"username": "admin",
                                                  "password": "adminpass123"}
                         ).status_code == 200


def test_locked_response_identical_for_existing_and_unknown_account(
        tmp_path, fake_embedder, monkeypatch):
    """锁定响应在"真账号被爆"与"不存在的用户名被爆"下完全一致：状态码、
    Retry-After、正文逐字节相同——429 只说明"这个用户名/IP 最近失败太多"，
    不透露账号是否存在（防账号枚举）。"""
    def _attack(dirname, username, password):
        d = tmp_path / dirname
        d.mkdir()
        app, _ = _client_on(d, fake_embedder, admin_password="adminpass123",
                            monkeypatch=monkeypatch)
        c = TestClient(app, client=("10.0.0.9", 40009))    # 两份库、同一来源 IP
        assert _fail_login(c, username, 5).status_code == 401
        return c.post("/api/auth/login", json={"username": username,
                                               "password": password})

    real = _attack("real", "admin", "adminpass123")        # 真账号 + 正确密码
    ghost = _attack("ghost", "ghost-user", "adminpass123")  # 不存在的用户名
    assert real.status_code == ghost.status_code == 429
    assert real.headers["Retry-After"] == ghost.headers["Retry-After"] == "30"
    assert real.content == ghost.content
    # 两边的判定依据也一致（用户名/ IP 两个维度的计数相同）
    assert [d["username_attempts"] for d in _lock_details(tmp_path / "real")] == [5]
    assert [d["username_attempts"] for d in _lock_details(tmp_path / "ghost")] == [5]


def test_forgot_and_reset_locked_by_ip(tmp_path, fake_embedder, monkeypatch):
    """spec 第 5 条：同一 IP 的登录失败计数同样作用于 forgot / reset。两处都
    只按 IP 判定（不看账号），所以连"账号存不存在"也不在响应里体现。"""
    # 发信是后台任务，测试里换成空实现（TestClient 会同步跑完它）
    monkeypatch.setattr("kbase.mailer.send_mail", lambda *a, **kw: None)

    def _attack(dirname, account):
        """同样的攻击序列、同样的来源 IP，唯一差别是 account 存不存在。"""
        d = tmp_path / dirname
        d.mkdir()
        app, _ = _client_on(d, fake_embedder, admin_password="adminpass123",
                            monkeypatch=monkeypatch)
        c = TestClient(app, client=("10.0.0.5", 40005))
        for i in range(5):
            c.post("/api/auth/login",
                   json={"username": f"guess{i}", "password": "x"})
        return app, c, c.post("/api/auth/forgot", json={"account": account})

    app_real, c_real, real = _attack("real", "admin")
    _, _, ghost = _attack("ghost", "no.such.user")
    assert real.status_code == ghost.status_code == 429
    assert real.headers["Retry-After"] == ghost.headers["Retry-After"] == "30"
    assert real.content == ghost.content            # 只认 IP，不看账号

    # 同一个 IP 的 reset 同样被拦；此时该 IP 已挨过一次拒绝 → 退避再翻一倍
    r = c_real.post("/api/auth/reset", json={"token": "x" * 43,
                                            "new_password": "newpw789"})
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
    # 锁事件留痕：三个端点共用一个 action，detail.endpoint 标出来源
    endpoints = {d["endpoint"] for d in _lock_details(tmp_path / "real")}
    assert endpoints == {"forgot", "reset"}
    # 换个 IP 一切正常（闸只锁被爆的那一个 IP）
    clean = TestClient(app_real, client=("10.0.0.6", 40006))
    assert clean.post("/api/auth/forgot", json={"account": "admin"}
                      ).status_code == 200


def test_login_guard_thresholds_come_from_config(tmp_path, fake_embedder,
                                                 monkeypatch, guard_clock):
    """阈值/窗口/退避都来自 config.login_guard（这里 2 次 / 60s / base 5 /
    上限 20，验证四个键真的被读，且窗口到点即解）。"""
    monkeypatch.setenv("KBASE_ADMIN_PASSWORD", "adminpass123")
    cfg = tmp_path / "kbase.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")) + """
login_guard:
  max_attempts: 2
  window_seconds: 60
  backoff_base_seconds: 5
  backoff_max_seconds: 20
""", encoding="utf-8")
    app = create_app(config_path=cfg, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="on")
    c = TestClient(app)
    assert _fail_login(c, "admin", 2).status_code == 401
    waits = []
    for _ in range(3):
        r = c.post("/api/auth/login", json={"username": "admin",
                                            "password": "adminpass123"})
        assert r.status_code == 429
        waits.append(int(r.headers["Retry-After"]))
    assert waits == [5, 10, 20]          # 5·2^0 / 5·2^1 / 封顶 20
    guard_clock.offset = 65              # 窗口 60s 滑过 → 解锁
    assert c.post("/api/auth/login", json={"username": "admin",
                                           "password": "adminpass123"}
                  ).status_code == 200
