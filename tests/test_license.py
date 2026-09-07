"""kbase/license.py 单测：Ed25519 验签、四态（trial/valid/expired/invalid）。

license.json 路径可通过 env KBASE_LICENSE_FILE 指定（测试用 tmp_path 隔离，
不触碰仓库根目录真实文件）。

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
"""
import base64
import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from kbase import license as license_mod
from scripts.gen_license import (generate_keypair, public_key_to_b64,
                                 sign_license)
from tests.test_auth import _client_on

# Ed25519 原始公钥 32 字节 → base64 恰好 44 字符（含尾部 ==）
_ED25519_PUBKEY_B64_LEN = 44


def _write_license(path, org, expires, private_key):
    license_dict = sign_license(org, expires, private_key)
    path.write_text(json.dumps(license_dict, ensure_ascii=False), encoding="utf-8")


def _patched_env(tmp_path, monkeypatch):
    """把 license 文件路径隔离到 tmp_path，返回该路径。"""
    license_path = tmp_path / "license.json"
    monkeypatch.setenv("KBASE_LICENSE_FILE", str(license_path))
    return license_path


def test_trial_when_no_license_file(tmp_path, monkeypatch):
    _patched_env(tmp_path, monkeypatch)
    result = license_mod.check_license()
    assert result["status"] == "trial"


def test_valid_license_with_future_expiry(tmp_path, monkeypatch):
    private_key, public_key = generate_keypair()
    # 让内置公钥认这把私钥：monkeypatch 还原后其他用例不受影响
    monkeypatch.setattr(license_mod, "_PUBLIC_KEY_B64", public_key_to_b64(public_key))
    license_path = _patched_env(tmp_path, monkeypatch)
    future = (date.today() + timedelta(days=30)).isoformat()
    _write_license(license_path, "测试客户", future, private_key)

    result = license_mod.check_license()
    assert result == {"status": "valid", "org": "测试客户", "expires": future}


def test_expired_license_past_expiry(tmp_path, monkeypatch):
    private_key, public_key = generate_keypair()
    monkeypatch.setattr(license_mod, "_PUBLIC_KEY_B64", public_key_to_b64(public_key))
    license_path = _patched_env(tmp_path, monkeypatch)
    past = (date.today() - timedelta(days=1)).isoformat()
    _write_license(license_path, "测试客户", past, private_key)

    result = license_mod.check_license()
    assert result == {"status": "expired", "org": "测试客户", "expires": past}


def test_invalid_license_wrong_signature(tmp_path, monkeypatch):
    """用另一把（未打进公钥常量的）私钥签发——签名验证不过，落 invalid。"""
    wrong_private_key, _wrong_public_key = generate_keypair()
    license_path = _patched_env(tmp_path, monkeypatch)
    future = (date.today() + timedelta(days=30)).isoformat()
    _write_license(license_path, "测试客户", future, wrong_private_key)

    result = license_mod.check_license()
    assert result == {"status": "invalid"}


def test_invalid_license_corrupted_signature_string(tmp_path, monkeypatch):
    """签发后手工损坏 signature 字符串——同样落 invalid。"""
    private_key, public_key = generate_keypair()
    monkeypatch.setattr(license_mod, "_PUBLIC_KEY_B64", public_key_to_b64(public_key))
    license_path = _patched_env(tmp_path, monkeypatch)
    future = (date.today() + timedelta(days=30)).isoformat()
    license_dict = sign_license("测试客户", future, private_key)
    license_dict["signature"] = license_dict["signature"][:-4] + "abcd"
    license_path.write_text(json.dumps(license_dict, ensure_ascii=False),
                            encoding="utf-8")

    result = license_mod.check_license()
    assert result == {"status": "invalid"}


def test_get_license_endpoint_reflects_trial_state(tmp_path, fake_embedder, monkeypatch):
    """GET /api/license（viewer 楼层——任意已登录角色都能查看）返回
    check_license() 的结果形状。用 auth="on" 应用+viewer 角色贯通一次，
    确认路由的最低角色声明是 viewer 而不是更高的 editor/admin。"""
    _patched_env(tmp_path, monkeypatch)
    app, c = _client_on(tmp_path, fake_embedder, admin_password="adminpass123",
                        monkeypatch=monkeypatch)
    c.post("/api/auth/login", json={"username": "admin", "password": "adminpass123"})
    viewer_key_resp = c.post("/api/settings/api-keys",
                             json={"name": "viewer-key", "role": "viewer"})
    viewer_key = viewer_key_resp.json()["key"]

    anon = TestClient(app)
    r = anon.get("/api/license", headers={"Authorization": f"Bearer {viewer_key}"})
    assert r.status_code == 200
    assert r.json() == {"status": "trial"}


def test_invalid_license_malformed_json(tmp_path, monkeypatch):
    license_path = _patched_env(tmp_path, monkeypatch)
    license_path.write_text("not valid json{{{", encoding="utf-8")

    result = license_mod.check_license()
    assert result == {"status": "invalid"}


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
    assert result == {"status": "invalid"}
