"""kbase/license.py 单测：Ed25519 验签、四态（trial/valid/expired/invalid）。
license.json 路径可通过 env KBASE_LICENSE_FILE 指定（测试用 tmp_path 隔离，
不触碰仓库根目录真实文件）。valid/expired 态用本次任务生成、保存在仓库外的
真实私钥（D:\\Claude Code\\kbase-license-private.pem）通过
scripts/gen_license.py 的函数现签，让 license.py 内置的公钥常量能验证通过；
invalid 态用另一把随手生成的密钥对签名（对不上内置公钥）或直接损坏签名字符串。
"""
import json
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from kbase import license as license_mod
from scripts.gen_license import generate_keypair, sign_license
from tests.test_auth import _client_on


REAL_PRIVATE_KEY_PATH = Path(r"D:\Claude Code\kbase-license-private.pem")


def _load_real_private_key():
    """kbase/license.py 内置的公钥常量对应的真实私钥——生成于本次任务，
    刻意保存在仓库之外（见 D:\\Claude Code\\kbase-license-private.pem，
    from README 与任务报告）。valid/expired 态必须用这把真实私钥签发，
    license.py 才能用它内置的公钥验签通过；用随手新生成的密钥对签出的
    证书应该落入 invalid 态（签名对不上），这个场景在
    test_invalid_license_wrong_signature 里单独覆盖。"""
    from scripts.gen_license import load_private_key_pem
    return load_private_key_pem(REAL_PRIVATE_KEY_PATH.read_bytes())


def _write_license(path, org, expires, private_key):
    license_dict = sign_license(org, expires, private_key)
    path.write_text(json.dumps(license_dict, ensure_ascii=False), encoding="utf-8")


def test_trial_when_no_license_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KBASE_LICENSE_FILE", str(tmp_path / "license.json"))
    result = license_mod.check_license()
    assert result["status"] == "trial"


def test_valid_license_with_future_expiry(tmp_path, monkeypatch):
    private_key = _load_real_private_key()
    license_path = tmp_path / "license.json"
    future = (date.today() + timedelta(days=30)).isoformat()
    _write_license(license_path, "测试客户", future, private_key)
    monkeypatch.setenv("KBASE_LICENSE_FILE", str(license_path))

    result = license_mod.check_license()
    assert result == {"status": "valid", "org": "测试客户", "expires": future}


def test_expired_license_past_expiry(tmp_path, monkeypatch):
    private_key = _load_real_private_key()
    license_path = tmp_path / "license.json"
    past = (date.today() - timedelta(days=1)).isoformat()
    _write_license(license_path, "测试客户", past, private_key)
    monkeypatch.setenv("KBASE_LICENSE_FILE", str(license_path))

    result = license_mod.check_license()
    assert result == {"status": "expired", "org": "测试客户", "expires": past}


def test_invalid_license_wrong_signature(tmp_path, monkeypatch):
    """用另一把（非内置公钥对应的）私钥签发——签名验证不过，落 invalid。"""
    wrong_private_key, _wrong_public_key = generate_keypair()
    license_path = tmp_path / "license.json"
    future = (date.today() + timedelta(days=30)).isoformat()
    _write_license(license_path, "测试客户", future, wrong_private_key)
    monkeypatch.setenv("KBASE_LICENSE_FILE", str(license_path))

    result = license_mod.check_license()
    assert result == {"status": "invalid"}


def test_invalid_license_corrupted_signature_string(tmp_path, monkeypatch):
    """用真实私钥签发后手工损坏 signature 字符串——同样落 invalid。"""
    private_key = _load_real_private_key()
    license_path = tmp_path / "license.json"
    future = (date.today() + timedelta(days=30)).isoformat()
    license_dict = sign_license("测试客户", future, private_key)
    license_dict["signature"] = license_dict["signature"][:-4] + "abcd"
    license_path.write_text(json.dumps(license_dict, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("KBASE_LICENSE_FILE", str(license_path))

    result = license_mod.check_license()
    assert result == {"status": "invalid"}


def test_get_license_endpoint_reflects_trial_state(tmp_path, fake_embedder, monkeypatch):
    """GET /api/license（viewer 楼层——任意已登录角色都能查看）返回
    check_license() 的结果形状。用 auth="on" 应用+viewer 角色贯通一次，
    确认路由的最低角色声明是 viewer 而不是更高的 editor/admin。"""
    monkeypatch.setenv("KBASE_LICENSE_FILE", str(tmp_path / "license.json"))
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
    license_path = tmp_path / "license.json"
    license_path.write_text("not valid json{{{", encoding="utf-8")
    monkeypatch.setenv("KBASE_LICENSE_FILE", str(license_path))

    result = license_mod.check_license()
    assert result == {"status": "invalid"}
