"""生成 KBase 轻量许可证：Ed25519 签发 license.json，供 kbase/license.py 验签。

用法（首次生成密钥对 + 签发一份许可证）：

    .venv\\Scripts\\python scripts\\gen_license.py --org "客户名称" --expires 2027-07-06 \\
        --private-key D:\\Claude Code\\kbase-license-private.pem --out license.json

若 --private-key 指向的文件不存在，会自动生成一个新的 Ed25519 密钥对，把
私钥写到该路径（**务必设在仓库之外，绝不提交到 git**），并把对应的公钥
打印到终端——需要手工把这段公钥粘贴进 kbase/license.py 的 _PUBLIC_KEY_B64
常量（该常量只信任与之匹配的私钥签出的证书，重新生成密钥对后旧证书全部
失效，需要重新签发）。

license.json 内容：{"org": str, "expires": "YYYY-MM-DD", "signature": base64}。
签名对象是 canonical JSON（sort_keys, 无多余空格）编码后的 {"org", "expires"}
两个字段，与 kbase/license.py 的验签逻辑必须完全一致（字段增减需两边同改）。
"""
import argparse
import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (Ed25519PrivateKey,
                                                                Ed25519PublicKey)


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


def private_key_to_pem(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())


def load_private_key_pem(pem_bytes: bytes) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(pem_bytes, password=None)


def public_key_to_b64(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def _signing_payload(org: str, expires: str) -> bytes:
    """签名对象的 canonical 编码——必须与 kbase/license.py 的验签逻辑一致。"""
    return json.dumps({"org": org, "expires": expires},
                      sort_keys=True, ensure_ascii=False).encode("utf-8")


def sign_license(org: str, expires: str, private_key: Ed25519PrivateKey) -> dict:
    """签发一份许可证字典（org/expires/signature），signature 是对
    _signing_payload(org, expires) 的 Ed25519 签名，base64 编码。"""
    payload = _signing_payload(org, expires)
    signature = private_key.sign(payload)
    return {"org": org, "expires": expires,
            "signature": base64.b64encode(signature).decode("ascii")}


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="生成 KBase 许可证密钥对（如需要）并签发 license.json")
    parser.add_argument("--org", required=True, help="客户/组织名称")
    parser.add_argument("--expires", required=True, help="到期日期 YYYY-MM-DD")
    parser.add_argument("--private-key", required=True,
                        help="私钥 PEM 文件路径（不存在则自动生成，务必设在仓库之外）")
    parser.add_argument("--out", default="license.json", help="输出的 license.json 路径")
    args = parser.parse_args()

    key_path = Path(args.private_key)
    if key_path.exists():
        private_key = load_private_key_pem(key_path.read_bytes())
        print(f"已加载现有私钥：{key_path}")
    else:
        private_key, public_key = generate_keypair()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(private_key_to_pem(private_key))
        print(f"已生成新密钥对，私钥已写入：{key_path}")
        print("请妥善保管此私钥文件（不要提交到 git / 不要放进仓库目录）。")
        print(f"对应公钥（base64，粘贴进 kbase/license.py 的 _PUBLIC_KEY_B64）：\n"
              f"{public_key_to_b64(public_key)}")

    license_dict = sign_license(args.org, args.expires, private_key)
    out_path = Path(args.out)
    out_path.write_text(json.dumps(license_dict, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"已签发许可证：{out_path}（org={args.org}, expires={args.expires}）")


if __name__ == "__main__":
    _main()
