"""kbase/auth/security.py 单测：密码哈希、JWT 签发/校验、secret 持久化、API Key 生成/校验。"""
import time

import jwt
import pytest

from kbase.auth import security
from kbase.db import make_session_factory


def test_password_hash_roundtrip_and_bad_password():
    hashed = security.hash_password("correct-horse")
    assert hashed != "correct-horse"
    assert security.verify_password("correct-horse", hashed)
    assert not security.verify_password("wrong-password", hashed)


def test_jwt_roundtrip_carries_claims():
    token = security.create_session_token("alice", "admin", secret="s3cr3t")
    payload = security.decode_session_token(token, secret="s3cr3t")
    assert payload["sub"] == "alice"
    assert payload["role"] == "admin"


def test_jwt_expiry_is_7_days():
    token = security.create_session_token("alice", "admin", secret="s3cr3t")
    payload = jwt.decode(token, "s3cr3t", algorithms=["HS256"])
    assert payload["exp"] - payload["iat"] == 7 * 24 * 3600


def test_jwt_expired_token_rejected():
    now = int(time.time())
    expired = jwt.encode(
        {"sub": "alice", "role": "admin", "iat": now - 1000, "exp": now - 1},
        "s3cr3t", algorithm="HS256")
    with pytest.raises(security.InvalidTokenError):
        security.decode_session_token(expired, secret="s3cr3t")


def test_jwt_tampered_token_rejected():
    # 篡改 payload 段中间的一个字符（而非末位）：base64url 末位字符只编码
    # 部分 bit，翻转它有时解码结果不变，会让测试偶发误通过；改在中段翻转，
    # 必然改变该段解码字节，签名必然失配。
    token = security.create_session_token("alice", "admin", secret="s3cr3t")
    header, payload, signature = token.split(".")
    mid = len(payload) // 2
    flipped_char = "a" if payload[mid] != "a" else "b"
    tampered_payload = payload[:mid] + flipped_char + payload[mid + 1:]
    tampered = ".".join([header, tampered_payload, signature])
    with pytest.raises(security.InvalidTokenError):
        security.decode_session_token(tampered, secret="s3cr3t")


def test_jwt_wrong_secret_rejected():
    token = security.create_session_token("alice", "admin", secret="s3cr3t")
    with pytest.raises(security.InvalidTokenError):
        security.decode_session_token(token, secret="other-secret")


def test_secret_resolution_prefers_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KBASE_SECRET_KEY", "from-env")
    sf = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    assert security.resolve_secret_key(sf) == "from-env"


def test_secret_resolution_generates_and_persists(tmp_path, monkeypatch):
    monkeypatch.delenv("KBASE_SECRET_KEY", raising=False)
    sf = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    first = security.resolve_secret_key(sf)
    second = security.resolve_secret_key(sf)
    assert first == second     # 重启（同一 DB 再次解析）应稳定
    assert len(first) >= 32


def test_secret_resolution_new_db_generates_different_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("KBASE_SECRET_KEY", raising=False)
    sf1 = make_session_factory(f"sqlite:///{tmp_path}/a.sqlite")
    sf2 = make_session_factory(f"sqlite:///{tmp_path}/b.sqlite")
    assert security.resolve_secret_key(sf1) != security.resolve_secret_key(sf2)


def test_generate_api_key_format():
    full_key, prefix, key_hash = security.generate_api_key()
    assert full_key.startswith("kbase_ak_")
    assert len(full_key) == len("kbase_ak_") + 32
    assert prefix == full_key[:8]
    assert key_hash == security.hash_api_key(full_key)


def test_api_key_verify_matches_hash():
    full_key, _prefix, key_hash = security.generate_api_key()
    assert security.hash_api_key(full_key) == key_hash
    assert security.hash_api_key("kbase_ak_wrong") != key_hash


def test_generate_api_key_is_random():
    k1, _, _ = security.generate_api_key()
    k2, _, _ = security.generate_api_key()
    assert k1 != k2
