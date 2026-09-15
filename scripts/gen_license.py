"""生成 KBase 轻量许可证：Ed25519 签发 license.json，供 kbase/license.py 验签。

用法（首次生成密钥对 + 签发一份许可证）：

    .venv\\Scripts\\python scripts\\gen_license.py --org "客户名称" --expires 2027-07-06 \\
        --private-key D:\\Claude Code\\kbase-license-private.pem --out license.json

T16 起可选签 v2 格式（带版本/座位数/功能位）：

    python scripts/gen_license.py --org "客户A" --expires 2027-07-06 \\
        --edition enterprise --seats 200 --feature governance --feature sso \\
        --private-key ... --out license.json

**不带 --edition/--seats/--feature 时签出的仍是 v1 格式**
（{"org","expires","signature"}），与改造前逐字节一致——已有文档、脚本和
owner 手上的演示证都按 v1 走，不能因为加了新版就把老的签发路径改掉。

若 --private-key 指向的文件不存在，会自动生成一个新的 Ed25519 密钥对，把
私钥写到该路径（**务必设在仓库之外，绝不提交到 git**），并把对应的公钥
打印到终端——需要手工把这段公钥粘贴进 kbase/license.py 的 _PUBLIC_KEY_B64
常量（该常量只信任与之匹配的私钥签出的证书，重新生成密钥对后旧证书全部
失效，需要重新签发）。

签名对象是 canonical JSON（sort_keys, 无多余空格，UTF-8 编码）编码后的
**除 signature 外的全部字段**：
- v1：{"org", "expires"}
- v2：{"org", "expires", "edition", "seats"[, "features"]}
与 kbase/license.py 的 _signable_payload 必须逐字节一致（字段增减需两边同改）。
"""
import argparse
import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (Ed25519PrivateKey,
                                                                Ed25519PublicKey)

# T16 功能位清单——与 kbase/license.py 的 KNOWN_FEATURES 同一份口径
# （拼错的功能位签出来谁也校验不到，等于白发一个功能）。
KNOWN_FEATURES = ("governance", "sso", "audit_export", "ops_attribution",
                  "answer_eval", "bundle_export")


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


def _signable_payload(license_dict: dict) -> bytes:
    """签名对象的 canonical 编码——必须与 kbase/license.py 的验签逻辑一致：
    除 signature 外的全部字段（v1 只有 org/expires，v2 多 edition/seats/
    features），sort_keys + ensure_ascii=False + 无多余空格。"""
    return json.dumps({k: v for k, v in license_dict.items() if k != "signature"},
                      sort_keys=True, ensure_ascii=False).encode("utf-8")


def sign_license(org: str, expires: str, private_key: Ed25519PrivateKey,
                 edition: str | None = None, seats: int | None = None,
                 features: list[str] | None = None) -> dict:
    """签发一份许可证字典。

    edition/seats 都为 None 且 features 为空 → **v1 形状**
    （{"org","expires","signature"}），与 T16 之前完全一致；
    否则 → v2 形状（含 edition/seats，features 非空时才带该字段）。
    features 去重并排序后写入，保证"同一组功能位"签出的字节稳定。
    """
    license_dict: dict = {"org": org, "expires": expires}
    if edition is not None or seats is not None:
        license_dict["edition"] = edition
        license_dict["seats"] = seats
    if features:
        license_dict["features"] = sorted(set(features))
    signature = private_key.sign(_signable_payload(license_dict))
    license_dict["signature"] = base64.b64encode(signature).decode("ascii")
    return license_dict


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="生成 KBase 许可证密钥对（如需要）并签发 license.json")
    parser.add_argument("--org", required=True, help="客户/组织名称")
    parser.add_argument("--expires", required=True, help="到期日期 YYYY-MM-DD")
    parser.add_argument("--private-key", required=True,
                        help="私钥 PEM 文件路径（不存在则自动生成，务必设在仓库之外）")
    parser.add_argument("--out", default="license.json", help="输出的 license.json 路径")
    # T16：三个 v2 字段都可选；全不传 = 签 v1（老文档/老脚本的调用方式不变）
    parser.add_argument("--edition", default=None,
                        help="版本标识（如 standard/enterprise）；传了即签 v2")
    parser.add_argument("--seats", type=int, default=None,
                        help="授权座位数（v2 必填位，须与 --edition 同给）")
    parser.add_argument("--feature", action="append", default=None,
                        dest="features", metavar="NAME",
                        help=f"功能位，可重复；可选值: {', '.join(KNOWN_FEATURES)}")
    args = parser.parse_args()

    features = args.features or None
    # v2 的 edition/seats 必须成对：只给一个会签出"有版本没座位"（或反过来）
    # 的证书，kbase/license.py 会直接判 invalid——那种证书发出去客户当场
    # 用不了，所以在这里就拦下。
    if (args.edition is None) != (args.seats is None):
        parser.error("--edition 与 --seats 必须同时提供（v2 证书的两个必填位）")
    if args.seats is not None and args.seats <= 0:
        parser.error("--seats 必须是正整数")
    if features and args.edition is None:
        parser.error("--feature 只能在 v2 证书（--edition/--seats）上使用"
                     "——v1 的签名对象里没有 features 字段")
    unknown = [f for f in features or [] if f not in KNOWN_FEATURES]
    if unknown:
        parser.error(f"未知功能位: {', '.join(unknown)}"
                     f"（可选值: {', '.join(KNOWN_FEATURES)}）")

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

    license_dict = sign_license(args.org, args.expires, private_key,
                                edition=args.edition, seats=args.seats,
                                features=features)
    out_path = Path(args.out)
    out_path.write_text(json.dumps(license_dict, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    fmt = "v2" if "edition" in license_dict else "v1"
    extra = (f", edition={args.edition}, seats={args.seats}, "
             f"features={license_dict.get('features') or '-'}"
             if fmt == "v2" else "（v1 格式，未指定 edition/seats/feature）")
    print(f"已签发许可证：{out_path}（org={args.org}, expires={args.expires}{extra}）")
    print(f"格式：{fmt}")


if __name__ == "__main__":
    _main()
