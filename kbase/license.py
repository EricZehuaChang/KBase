"""轻量许可证校验：Ed25519 验签 license.json，四态 trial/valid/expired/invalid。

不锁功能（v1 商务友好，见 spec §6/§8）——校验结果只用于 GET /api/license
展示状态，从不拦截任何业务请求。

license.json 路径：env KBASE_LICENSE_FILE 优先；否则默认仓库根目录下的
license.json（未提交到 git，客户私有化部署时按需放置，见
scripts/gen_license.py）。

签名对象与 scripts/gen_license.py 的 _signing_payload 必须保持一致：对
{"org", "expires"} 两个字段的 canonical JSON（sort_keys, 无多余空格，
UTF-8 编码）做 Ed25519 签名。
"""
import base64
import json
import os
from datetime import date
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# 与私钥配对的公钥（scripts/gen_license.py 生成时打印）。私钥保存在仓库
# 之外（D:\Claude Code\kbase-license-private.pem，不提交到 git），只有
# 持有该私钥才能签出这里能验证通过的 license.json。
_PUBLIC_KEY_B64 = "IqOgil+VsdctRy7g6YjUnEtDQT8gbof5dn9MpjPTHfI="

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LICENSE_PATH = _REPO_ROOT / "license.json"


def _license_path() -> Path:
    env_path = os.environ.get("KBASE_LICENSE_FILE")
    return Path(env_path) if env_path else _DEFAULT_LICENSE_PATH


def _signing_payload(org: str, expires: str) -> bytes:
    return json.dumps({"org": org, "expires": expires},
                      sort_keys=True, ensure_ascii=False).encode("utf-8")


def _verify_signature(org: str, expires: str, signature_b64: str) -> bool:
    public_key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(_PUBLIC_KEY_B64))
    try:
        public_key.verify(base64.b64decode(signature_b64),
                          _signing_payload(org, expires))
        return True
    except (InvalidSignature, ValueError):
        return False


def check_license() -> dict:
    """返回 {"status": "trial"|"valid"|"expired"|"invalid", "org": ..., "expires": ...}。
    org/expires 仅在文件存在且能解析出这两个字段时附带（trial 态没有文件，
    没有这两个 key；invalid 态若字段本身就缺失/格式错也不附带）。"""
    path = _license_path()
    if not path.exists():
        return {"status": "trial"}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        org = data["org"]
        expires = data["expires"]
        signature = data["signature"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {"status": "invalid"}

    if not _verify_signature(org, expires, signature):
        return {"status": "invalid"}

    try:
        expires_date = date.fromisoformat(expires)
    except ValueError:
        return {"status": "invalid"}

    if expires_date < date.today():
        return {"status": "expired", "org": org, "expires": expires}
    return {"status": "valid", "org": org, "expires": expires}
