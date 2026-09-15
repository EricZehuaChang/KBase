"""kbase/license.py 单测：Ed25519 验签、四态（trial/valid/expired/invalid）。

license.json 路径可通过 env KBASE_LICENSE_FILE 指定（测试用 tmp_path 隔离，
不触碰仓库根目录真实文件）。**T16 起换 env 后必须调 license_mod.configure()
刷新缓存**——生效配置（开关/宽限期/证书路径）按进程缓存，不跨用例残留。

valid/expired/corrupted 态**不再依赖仓库外私钥文件**（原测试硬编码
D:\\Claude Code\\kbase-license-private.pem，导致任何没有该私钥的机器——
CI、换机——上三个用例直接 FileNotFoundError）。改为：每个用例现生成一把
Ed25519 密钥对，把对应公钥 monkeypatch 进 kbase.license._PUBLIC_KEY_B64
（_verify_signature 每次调用时都读该常量），再用同一把私钥现签——完全不
出网、不落盘、跨平台。

内置公钥常量的完整性仍要盯：test_builtin_public_key_is_well_formed 校验
格式；test_builtin_key_rejects_generated_signature 证明"没有真私钥的人签的
字验证不过"。用真私钥回验内置常量属于本机/持钥者手动流程
（scripts/gen_license.py --private-key），不进自动测试。

T16 新增覆盖（产品负责人拍板："授权到期，直接拦截，现阶段当前不拦截，留一个
开关给我，没有交付客户"——所以拦截代码必须真实可用、且要测到）：
- v2 格式（edition/seats/features，签名覆盖全部非 signature 字段）；
- v1 老证书仍能验证（否则一上线所有已签发证书作废）；
- 篡改 v2 的 edition → invalid（签名外字段不可改）；
- enforce=false 不拦、enforce=true 到期未超宽限期不拦、超宽限期 402；
- 豁免清单真的可达（登录/查状态/healthz），且**离线续期全链路走得通**：
  到期被拦 → 登录 → 上传新证书 → 放行。
"""
import base64
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kbase import license as license_mod
from kbase.api.main import create_app
from scripts.gen_license import (generate_keypair, public_key_to_b64,
                                 sign_license)
from tests.test_api import CFG, FakeLLM
from tests.test_auth import _client_on

# Ed25519 原始公钥 32 字节 → base64 恰好 44 字符（含尾部 ==）
_ED25519_PUBKEY_B64_LEN = 44

_ADMIN_PASSWORD = "adminpass123"


@pytest.fixture(autouse=True)
def _reset_license_state():
    """每条用例前后清空 license 模块的进程级状态（生效配置缓存 + 证书缓存）。

    不清的话：create_app 会把 _config_path 指到上一个用例的 tmp 目录，
    下一个用例的 enforce/证书路径就成了别人的残留——这类"串味"故障在
    license 这种全局模块上最难查。"""
    def _clear():
        license_mod._config_path = None
        license_mod._config_cache = None
        license_mod.invalidate_license_cache()

    _clear()
    yield
    _clear()


# --------------------------------------------------------------- 测试脚手架

def _write_license(path, org, expires, private_key):
    """签 v1 形状的证书（不带 edition/seats/feature 时 gen_license 就是 v1——
    这条路径本身就是"老文档/老脚本的调用方式不变"的回归证明）。"""
    license_dict = sign_license(org, expires, private_key)
    path.write_text(json.dumps(license_dict, ensure_ascii=False), encoding="utf-8")


def _write_license_v2(path, org, expires, private_key, *, edition="enterprise",
                      seats=50, features=None):
    license_dict = sign_license(org, expires, private_key, edition=edition,
                                seats=seats, features=features)
    path.write_text(json.dumps(license_dict, ensure_ascii=False), encoding="utf-8")
    return license_dict


def _patched_env(tmp_path, monkeypatch, *, enforce=False, grace_days=14):
    """把 license 文件路径与开关配置隔离到 tmp_path，返回证书路径。

    写一份只带 data_dir + license 段的最小 YAML 当配置源，并调 configure()
    刷新生效配置缓存（不刷新的话上一个用例的开关/证书路径会残留）。"""
    return _env_with(tmp_path, monkeypatch, enforce=enforce,
                     grace_days=grace_days)[0]


def _env_with(tmp_path, monkeypatch, *, enforce, grace_days=14):
    """同上，返回 (证书路径, 配置文件路径)。enforce 显式给，避免默读别人状态。"""
    license_path = tmp_path / "license.json"
    monkeypatch.setenv("KBASE_LICENSE_FILE", str(license_path))
    config_path = tmp_path / "kbase.yaml"
    cfg_text = CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/"))
    cfg_text += (f"license:\n  enforce: {str(enforce).lower()}\n"
                 f"  grace_days: {grace_days}\n")
    config_path.write_text(cfg_text, encoding="utf-8")
    license_mod.configure(config_path)
    return license_path, config_path


def _pki(monkeypatch):
    """现生成一把密钥对，并把公钥装进内置常量；返回 (私钥, 公钥b64)。"""
    private_key, public_key = generate_keypair()
    b64 = public_key_to_b64(public_key)
    monkeypatch.setattr(license_mod, "_PUBLIC_KEY_B64", b64)
    return private_key, b64


def _app_client(config_path, tmp_path, fake_embedder, monkeypatch):
    """构建 auth="on" 的真实应用（建 superadmin admin）。"""
    monkeypatch.setenv("KBASE_ADMIN_PASSWORD", _ADMIN_PASSWORD)
    app = create_app(config_path=config_path, embedder=fake_embedder,
                     llms={"fake": FakeLLM()}, reranker=False, auth="on")
    return app, TestClient(app)


def _login(c):
    r = c.post("/api/auth/login", json={"username": "admin",
                                        "password": _ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return r


# ------------------------------------------------------- 基础四态（v1 行为）

def test_trial_when_no_license_file(tmp_path, monkeypatch):
    _patched_env(tmp_path, monkeypatch)
    result = license_mod.check_license()
    assert result["status"] == "trial"


def test_valid_license_with_future_expiry(tmp_path, monkeypatch):
    private_key, _b64 = _pki(monkeypatch)
    license_path = _patched_env(tmp_path, monkeypatch)
    future = (date.today() + timedelta(days=30)).isoformat()
    _write_license(license_path, "测试客户", future, private_key)

    result = license_mod.check_license()
    # v1 证书：无版本/座位位，格式如实标 v1；未到期 → 无宽限期剩余
    assert result == {"status": "valid", "org": "测试客户", "expires": future,
                      "edition": None, "seats": None, "features": None,
                      "format": "v1", "grace_days_left": None}


def test_expired_license_past_expiry(tmp_path, monkeypatch):
    private_key, _b64 = _pki(monkeypatch)
    license_path = _patched_env(tmp_path, monkeypatch)
    past = (date.today() - timedelta(days=1)).isoformat()
    _write_license(license_path, "测试客户", past, private_key)

    result = license_mod.check_license()
    # 状态如实回 expired（拦不拦是 enforce 的事，见后方用例）；宽限期 14 天，
    # 过期 1 天 → 还剩 13 天
    assert result == {"status": "expired", "org": "测试客户", "expires": past,
                      "edition": None, "seats": None, "features": None,
                      "format": "v1", "grace_days_left": 13}


def test_invalid_license_wrong_signature(tmp_path, monkeypatch):
    """用另一把（未打进公钥常量的）私钥签发——签名验证不过，落 invalid。"""
    wrong_private_key, _wrong_public_key = generate_keypair()
    license_path = _patched_env(tmp_path, monkeypatch)
    future = (date.today() + timedelta(days=30)).isoformat()
    _write_license(license_path, "测试客户", future, wrong_private_key)

    result = license_mod.check_license()
    assert result["status"] == "invalid"
    assert result["format"] is None and result["org"] is None


def test_invalid_license_corrupted_signature_string(tmp_path, monkeypatch):
    """签发后手工损坏 signature 字符串——同样落 invalid。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path = _patched_env(tmp_path, monkeypatch)
    future = (date.today() + timedelta(days=30)).isoformat()
    license_dict = sign_license("测试客户", future, private_key)
    license_dict["signature"] = license_dict["signature"][:-4] + "abcd"
    license_path.write_text(json.dumps(license_dict, ensure_ascii=False),
                            encoding="utf-8")

    result = license_mod.check_license()
    assert result["status"] == "invalid"
    assert result["format"] is None


def test_get_license_endpoint_reflects_trial_state(tmp_path, fake_embedder, monkeypatch):
    """GET /api/license（viewer 楼层——任意已登录角色都能查看）返回
    check_license() 的结果形状。用 auth="on" 应用+viewer 角色贯通一次，
    确认路由的最低角色声明是 viewer 而不是更高的 editor/admin。"""
    _patched_env(tmp_path, monkeypatch)
    app, c = _client_on(tmp_path, fake_embedder, admin_password=_ADMIN_PASSWORD,
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin",
                                    "password": _ADMIN_PASSWORD})
    viewer_key_resp = c.post("/api/settings/api-keys",
                             json={"name": "viewer-key", "role": "viewer"})
    viewer_key = viewer_key_resp.json()["key"]

    anon = TestClient(app)
    r = anon.get("/api/license", headers={"Authorization": f"Bearer {viewer_key}"})
    assert r.status_code == 200
    # 没有证书 → trial；key 集合与 valid/expired 一致（值为 None）
    assert r.json() == {"status": "trial", "org": None, "expires": None,
                        "edition": None, "seats": None, "features": None,
                        "format": None, "grace_days_left": None}


def test_invalid_license_malformed_json(tmp_path, monkeypatch):
    license_path = _patched_env(tmp_path, monkeypatch)
    license_path.write_text("not valid json{{{", encoding="utf-8")

    result = license_mod.check_license()
    assert result["status"] == "invalid"
    assert result["format"] is None


def test_builtin_public_key_is_well_formed():
    """内置公钥常量是合法的 32 字节 Ed25519 原始公钥的 base64。

    这把公钥对应仓库外私钥（scripts/gen_license.py 生成时打印后手工粘贴），
    本测试不持私钥、无法验真伪，只能拦住"常量被误改/截断/整段丢失"这类
    漂移；真伪回验是持钥者的手动流程。"""
    raw = license_mod._PUBLIC_KEY_B64
    assert isinstance(raw, str) and len(raw) == _ED25519_PUBKEY_B64_LEN
    assert len(base64.b64decode(raw)) == 32


def test_builtin_key_rejects_generated_signature(tmp_path, monkeypatch):
    """没持有真私钥的人（现生成的密钥对）签的证书，内置公钥验证不过。

    与 test_builtin_public_key_is_well_formed 配合：前者证明常量格式没坏，
    后者证明验证路径真的在比对这把常量而不是形同虚设。"""
    fresh_private_key, _fresh_public_key = generate_keypair()
    license_path = _patched_env(tmp_path, monkeypatch)
    future = (date.today() + timedelta(days=30)).isoformat()
    _write_license(license_path, "冒名者", future, fresh_private_key)

    result = license_mod.check_license()
    assert result["status"] == "invalid"
    assert result["format"] is None


# ------------------------------------------------------------------ v2 格式

def test_v2_file_verifies_and_reports_edition_seats_features(tmp_path, monkeypatch):
    """v2：edition/seats/features 参与签名，验签通过并如实回传。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path = _patched_env(tmp_path, monkeypatch)
    future = (date.today() + timedelta(days=90)).isoformat()
    written = _write_license_v2(license_path, "企业客户", future, private_key,
                                edition="enterprise", seats=200,
                                features=["governance", "answer_eval"])
    # 签名字段之外的字段顺序/缩进不影响验证（canonical 编码只看键值）
    assert set(written) == {"org", "expires", "edition", "seats", "features",
                            "signature"}

    result = license_mod.check_license()
    assert result == {"status": "valid", "org": "企业客户", "expires": future,
                      "edition": "enterprise", "seats": 200,
                      "features": ["answer_eval", "governance"],
                      "format": "v2", "grace_days_left": None}


def test_v2_tampered_edition_is_invalid(tmp_path, monkeypatch):
    """篡改 v2 文件里的 edition（签名覆盖它）→ invalid。

    这是 v2 校验顺序的回归证明：若按 v1 的 {"org","expires"} 验签，改
    edition 不影响验签结果，客户就能自助把 standard 改成 enterprise。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path = _patched_env(tmp_path, monkeypatch)
    future = (date.today() + timedelta(days=90)).isoformat()
    data = _write_license_v2(license_path, "企业客户", future, private_key,
                             edition="standard", seats=50)
    data["edition"] = "enterprise"
    license_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert license_mod.check_license()["status"] == "invalid"


def test_v2_tampered_seats_is_invalid(tmp_path, monkeypatch):
    """同理：座位数也在签名里，改一个数就作废。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path = _patched_env(tmp_path, monkeypatch)
    future = (date.today() + timedelta(days=90)).isoformat()
    data = _write_license_v2(license_path, "企业客户", future, private_key,
                             seats=50)
    data["seats"] = 9999
    license_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert license_mod.check_license()["status"] == "invalid"


def test_v1_file_still_validates_after_v2_support(tmp_path, monkeypatch):
    """**老证书不能因为这次升级作废**：v1 文件（org/expires/signature）仍验证
    通过，且被如实标成 format=v1。owner 手上的演示证就是这份格式。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path = _patched_env(tmp_path, monkeypatch)
    future = (date.today() + timedelta(days=365)).isoformat()
    # 手工构造 v1 文件（不走 sign_license，确保字段集合就是历史那三个）
    payload = json.dumps({"org": "老客户", "expires": future},
                         sort_keys=True, ensure_ascii=False).encode("utf-8")
    signature = base64.b64encode(private_key.sign(payload)).decode("ascii")
    assert set(json.loads(json.dumps({"org": "老客户", "expires": future,
                                      "signature": signature}))) == {
        "org", "expires", "signature"}
    license_path.write_text(
        json.dumps({"org": "老客户", "expires": future, "signature": signature},
                   ensure_ascii=False), encoding="utf-8")

    result = license_mod.check_license()
    assert result["status"] == "valid"
    assert result["format"] == "v1"
    assert result["edition"] is None and result["seats"] is None
    # 而且**不受 enforce 影响**：v1 证书一样进拦截判定（下面另有用例）
    assert license_mod.effective_config().enforce is False


def test_v2_without_seats_is_invalid(tmp_path, monkeypatch):
    """v2 缺 seats 位（只签了 edition）→ invalid：签出说不清座位的证书是
    签发侧的问题，不能当"不限座位"放行。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path = _patched_env(tmp_path, monkeypatch)
    future = (date.today() + timedelta(days=30)).isoformat()
    data = {"org": "半个v2", "expires": future, "edition": "standard"}
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    data["signature"] = base64.b64encode(private_key.sign(payload)).decode("ascii")
    license_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert license_mod.check_license()["status"] == "invalid"


# --------------------------------------------------------------- 拦截（enforce）

def test_enforce_default_is_false_in_shipped_config():
    """**发出去的配置必须默认不拦**——产品负责人拍板"现阶段当前不拦截"。

    这里读真实仓库配置：standard 档（交付档）的 license.enforce 必须是
    false、grace_days 必须是 14。若哪天有人"顺手"把它改成 true，这个用例
    先红——那时候需要的是负责人重新拍板，而不是改测试。"""
    import yaml
    raw = yaml.safe_load(
        (Path(__file__).resolve().parents[1]
         / "config" / "kbase.standard.yaml").read_text(encoding="utf-8"))
    assert raw["license"]["enforce"] is False
    assert raw["license"]["grace_days"] == 14
    # lite 档（config/kbase.yaml，T17 正在改，暂未加该段）不配也应回落默认值，
    # 这一点由 test_config_section_absent_falls_back_to_defaults 保证。
    assert license_mod._DEFAULT_ENFORCE is False
    assert license_mod._DEFAULT_GRACE_DAYS == 14


def test_config_section_absent_falls_back_to_defaults(tmp_path, monkeypatch):
    """配置里没有 license 段（如尚未加该段的 lite 档）→ 默认不拦、宽限 14 天。"""
    monkeypatch.setenv("KBASE_LICENSE_FILE", str(tmp_path / "license.json"))
    cfg = tmp_path / "no-license-section.yaml"
    cfg.write_text(CFG.format(data_dir=str(tmp_path / "data").replace("\\", "/")),
                   encoding="utf-8")
    license_mod.configure(cfg)
    policy = license_mod.effective_config()
    assert (policy.enforce, policy.grace_days) == (False, 14)


def test_config_bad_values_fall_back_to_safe_defaults(tmp_path, monkeypatch):
    """写坏的开关值一律落回"不拦"：enforce 只认真正的 bool（"true" 字符串是
    手误，宽松猜测的方向是"以为拦着其实没拦"，比默认值更危险）。"""
    monkeypatch.setenv("KBASE_LICENSE_FILE", str(tmp_path / "license.json"))
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("license:\n  enforce: 'true'\n  grace_days: -5\n",
                   encoding="utf-8")
    license_mod.configure(cfg)
    policy = license_mod.effective_config()
    assert (policy.enforce, policy.grace_days) == (False, 14)


def test_enforce_false_expired_does_not_block(tmp_path, fake_embedder, monkeypatch):
    """enforce=false（本部署默认）：证书过期照样能用业务端点。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=False)
    past = (date.today() - timedelta(days=400)).isoformat()
    _write_license(license_path, "测试客户", past, private_key)
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)
    _login(c)

    assert c.get("/api/kb").status_code == 200
    assert c.get("/api/license").json()["status"] == "expired"


def test_enforce_true_expired_within_grace_not_blocked(tmp_path, fake_embedder,
                                                      monkeypatch):
    """enforce=true + 已过期但在宽限期内 → 不拦，且状态如实报 expired 与剩余天数。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=True,
                                          grace_days=14)
    past = (date.today() - timedelta(days=3)).isoformat()
    _write_license(license_path, "测试客户", past, private_key)
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)
    _login(c)

    assert c.get("/api/kb").status_code == 200
    body = c.get("/api/license").json()
    assert body["status"] == "expired"
    assert body["grace_days_left"] == 11


def test_enforce_true_beyond_grace_returns_402(tmp_path, fake_embedder, monkeypatch):
    """enforce=true + 超出宽限期 → 业务端点 402 license_expired。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=True,
                                          grace_days=14)
    past = (date.today() - timedelta(days=15)).isoformat()   # 14 天宽限已用尽
    _write_license(license_path, "测试客户", past, private_key)
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)
    _login(c)

    r = c.get("/api/kb")
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "error.license_expired"
    # 多打几个不同域的端点，确认不是只有 /api/kb 被拦
    assert c.get("/api/jobs").status_code == 402
    assert c.post("/api/kb", json={"name": "x"}).status_code == 402


def test_enforce_true_valid_license_not_blocked(tmp_path, fake_embedder, monkeypatch):
    """enforce=true + 未到期 → 正常放行（拦截不是"开了就全拦"）。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=True)
    future = (date.today() + timedelta(days=10)).isoformat()
    _write_license(license_path, "测试客户", future, private_key)
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)
    _login(c)

    assert c.get("/api/kb").status_code == 200


def test_allowlist_reachable_while_blocked(tmp_path, fake_embedder, monkeypatch):
    """到期被拦时，豁免清单**真的可达**（离线续期链路的可达性是硬要求）：
    登录端点、状态端点、healthz 全部不为 402；且**拦截只作用于 /api、/v1**——
    静态资源与前端路由照常 200，否则现场只能看到一个空窗口，连"上传新证书"
    那张页面都画不出来。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=True)
    past = (date.today() - timedelta(days=30)).isoformat()
    _write_license(license_path, "测试客户", past, private_key)
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)

    # 未登录也要能登录（凭据正确）
    r = c.post("/api/auth/login", json={"username": "admin",
                                        "password": _ADMIN_PASSWORD})
    assert r.status_code == 200
    assert c.get("/api/license").status_code == 200
    assert c.get("/api/license").json()["status"] == "expired"
    assert c.get("/healthz").status_code == 200
    # 前端 shell 与静态资源：不在拦截作用域内
    assert c.get("/kb").status_code == 200
    assert c.get("/").status_code == 200
    # 作用域判定（纯函数，逐条钉住边界）
    assert license_mod.in_blocked_scope("/api/kb") is True
    assert license_mod.in_blocked_scope("/api/auth/login") is False
    assert license_mod.in_blocked_scope("/api/license") is False
    assert license_mod.in_blocked_scope("/api/authentic") is True   # 不是登录端点
    assert license_mod.in_blocked_scope("/kb") is False
    assert license_mod.in_blocked_scope("/healthz") is False


def test_upload_endpoint_is_admin_only(tmp_path, fake_embedder, monkeypatch):
    """续期上传是 admin 专属：viewer 的 Key 403，未登录 401。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=True)
    license_path.write_text(
        json.dumps(_v2_dict(private_key, days=30, features=["governance"]),
                   ensure_ascii=False), encoding="utf-8")
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)
    _login(c)
    viewer_key = c.post("/api/settings/api-keys",
                        json={"name": "viewer-key", "role": "viewer"}).json()["key"]
    # 换成一份已到期的证书：此刻业务端点被拦，但只要上传端点仍然可达，
    # 管理员就有救（下面两条越权请求就发生在"被拦"状态下）
    _write_license(license_path, "测试客户", (date.today()
                                              - timedelta(days=30)).isoformat(),
                   private_key)
    license_mod.invalidate_license_cache()
    assert license_mod.enforcement_state()["blocked"] is True
    assert c.get("/api/kb").status_code == 402
    assert _login(c).status_code == 200

    fresh = json.dumps(_v2_dict(private_key, days=30), ensure_ascii=False)
    anon = TestClient(app)
    r = anon.post("/api/license",
                  headers={"Authorization": f"Bearer {viewer_key}"},
                  files={"file": ("license.json", fresh.encode("utf-8"),
                                  "application/json")})
    assert r.status_code == 403
    r2 = anon.post("/api/license",
                   files={"file": ("license.json", fresh.encode("utf-8"),
                                   "application/json")})
    assert r2.status_code == 401
    # 两次未授权请求都没把证书换掉：磁盘上还是原来那份过期证
    assert json.loads(license_path.read_text(encoding="utf-8"))["org"] == "测试客户"


def test_upload_rejects_unsigned_file_without_replacing(tmp_path, fake_embedder,
                                                        monkeypatch):
    """签不过的文件**不落盘**——不能因为上传出错把原本还能用的证书覆盖掉。"""
    private_key, _b64 = _pki(monkeypatch)
    other_private_key, _other_public = generate_keypair()
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=False)
    past = (date.today() - timedelta(days=30)).isoformat()
    _write_license(license_path, "测试客户", past, private_key)
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)
    _login(c)

    bogus = json.dumps(sign_license("冒名者", "2999-01-01", other_private_key),
                       ensure_ascii=False)
    r = c.post("/api/license",
               files={"file": ("license.json", bogus.encode("utf-8"),
                               "application/json")})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "error.license_bad_file"
    # 原文件原样保留
    assert json.loads(license_path.read_text(encoding="utf-8"))["org"] == "测试客户"

    # 非 JSON 也同样 422
    r2 = c.post("/api/license", files={"file": ("license.json", b"not json",
                                                "application/json")})
    assert r2.status_code == 422
    # 缺文件字段 → 422
    assert c.post("/api/license").status_code == 422


def _v2_dict(private_key, *, days=365, org="续期客户", edition="enterprise",
             seats=100, features=("governance",)):
    return sign_license(org, (date.today() + timedelta(days=days)).isoformat(),
                        private_key, edition=edition, seats=seats,
                        features=list(features))


def test_offline_renewal_round_trip(tmp_path, fake_embedder, monkeypatch):
    """**离线续期全链路**：到期被拦 → 管理员登录 → 上传新证书 → 业务恢复。

    这是 T16 最关键的一条：拦截必须真的能解开，否则授权一到期客户现场
    就只剩手工进机器拷文件一条路（客户的部署常常完全离线）。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=True,
                                          grace_days=14)
    past = (date.today() - timedelta(days=30)).isoformat()
    _write_license(license_path, "老证书", past, private_key)
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)

    # 1) 到期超出宽限期：业务端点 402
    _login(c)
    blocked = c.get("/api/kb")
    assert blocked.status_code == 402
    assert blocked.json()["detail"]["code"] == "error.license_expired"

    # 2) 豁免清单可达：登录态仍在、状态可查（前端据此提示续期）
    assert c.get("/api/license").json()["status"] == "expired"

    # 3) 上传新的 v2 证书（admin 会话）→ 原子替换 + 缓存立即失效
    fresh = _v2_dict(private_key, days=365, org="续期客户")
    up = c.post("/api/license",
                files={"file": ("license.json",
                                json.dumps(fresh, ensure_ascii=False).encode("utf-8"),
                                "application/json")})
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["ok"] is True
    assert body["license"]["status"] == "valid"
    assert body["license"]["org"] == "续期客户"
    assert body["license"]["edition"] == "enterprise"
    assert body["license"]["seats"] == 100
    assert body["license"]["format"] == "v2"
    assert json.loads(license_path.read_text(encoding="utf-8"))["org"] == "续期客户"

    # 4) 不用重启、不用改配置：下一个请求就放行
    assert c.get("/api/kb").status_code == 200


def test_renewal_with_expired_certificate_still_blocked(tmp_path, fake_embedder,
                                                        monkeypatch):
    """上传一份"签名有效但同样已过期"的证书：校验通过（是合法证书），但
    拦截不会因此解开——避免"随便传一份旧证就当续期成功"的错觉。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=True)
    past = (date.today() - timedelta(days=30)).isoformat()
    _write_license(license_path, "老证书", past, private_key)
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)
    _login(c)

    still_expired = json.dumps(
        sign_license("还是过期", (date.today() - timedelta(days=20)).isoformat(),
                     private_key, edition="standard", seats=10),
        ensure_ascii=False)
    up = c.post("/api/license",
                files={"file": ("license.json",
                                still_expired.encode("utf-8"), "application/json")})
    assert up.status_code == 200
    assert up.json()["license"]["status"] == "expired"
    assert c.get("/api/kb").status_code == 402


# ------------------------------------------------------------- 功能位（feature）

def test_require_feature_is_noop_when_enforce_false(tmp_path, fake_embedder,
                                                    monkeypatch):
    """enforce=false：功能位依赖不生效（不读文件、不查证书），端点照常可用。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=False)
    future = (date.today() + timedelta(days=30)).isoformat()
    # features 里故意**不含** governance
    _write_license_v2(license_path, "基础版", future, private_key,
                      features=["sso"])
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)
    _login(c)

    assert c.get("/api/settings/api-keys").status_code == 200
    assert license_mod.feature_enabled("governance") is True


def test_require_feature_blocks_missing_feature(tmp_path, fake_embedder, monkeypatch):
    """enforce=true 且证书 features 缺该功能位 → 402 license_feature_denied。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=True)
    future = (date.today() + timedelta(days=30)).isoformat()
    _write_license_v2(license_path, "基础版", future, private_key,
                      features=["sso", "audit_export"])
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)
    _login(c)

    r = c.get("/api/settings/api-keys")
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "error.license_feature_denied"
    assert r.json()["detail"]["params"]["feature"] == "governance"
    # 缺功能位只锁对应域，不锁整个应用（业务端点仍可用）
    assert c.get("/api/kb").status_code == 200


def test_require_feature_allows_listed_feature(tmp_path, fake_embedder, monkeypatch):
    """features 里含 governance → API Key 治理族照常可用。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=True)
    future = (date.today() + timedelta(days=30)).isoformat()
    _write_license_v2(license_path, "企业版", future, private_key,
                      features=["governance"])
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)
    _login(c)

    assert c.get("/api/settings/api-keys").status_code == 200
    created = c.post("/api/settings/api-keys",
                     json={"name": "k", "role": "viewer"})
    assert created.status_code == 200


def test_v1_certificate_does_not_lock_features(tmp_path, fake_embedder, monkeypatch):
    """v1 老证书没有 features 字段 = 不限功能：enforce=true 也不能因此把
    老客户的付费功能全锁掉（升级不降级）。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=True)
    future = (date.today() + timedelta(days=30)).isoformat()
    _write_license(license_path, "老客户", future, private_key)   # v1
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)
    _login(c)

    assert license_mod.check_license()["format"] == "v1"
    assert c.get("/api/settings/api-keys").status_code == 200


def test_grace_days_zero_blocks_on_expiry(tmp_path, fake_embedder, monkeypatch):
    """grace_days=0：到期当天即拦（宽限期是配置，不是硬编码）。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=True,
                                          grace_days=0)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    _write_license(license_path, "测试客户", yesterday, private_key)
    app, c = _app_client(config_path, tmp_path, fake_embedder, monkeypatch)
    _login(c)

    assert c.get("/api/kb").status_code == 402


def test_enforcement_state_does_not_reverify_file_each_call(tmp_path, monkeypatch):
    """缓存证明：文件不变时，多次判定只发生一次"读文件+验签"。

    统计 read_license_file 的调用次数——它是唯一会读盘+验签的入口，
    stat 命中缓存后不应再被调用（并发/多请求下的性能红线）。"""
    private_key, _b64 = _pki(monkeypatch)
    license_path, config_path = _env_with(tmp_path, monkeypatch, enforce=True)
    future = (date.today() + timedelta(days=30)).isoformat()
    _write_license(license_path, "测试客户", future, private_key)
    license_mod.invalidate_license_cache()

    calls = {"n": 0}
    real = license_mod.read_license_file

    def _counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(license_mod, "read_license_file", _counting)
    states = [license_mod.enforcement_state() for _ in range(20)]
    assert all(s["blocked"] is False for s in states)
    assert calls["n"] == 1, f"文件未变却被重新验签 {calls['n']} 次"

    # enforce=false 时连 stat 都不做：把路径换成不存在的文件也不影响判定
    monkeypatch.setattr(license_mod, "_config_cache", None)
    monkeypatch.setenv("KBASE_LICENSE_FILE", str(tmp_path / "missing.json"))
    license_mod.configure(config_path)
    before = calls["n"]
    for _ in range(20):
        assert license_mod.enforcement_state()["blocked"] is False
    assert calls["n"] == before
