"""认证安全原语：密码哈希（bcrypt）、会话 JWT（HS256）、secret 解析、API Key 生成/校验。

secret 解析顺序：env KBASE_SECRET_KEY 优先（生产建议显式设置，见 README）；
否则首次调用时生成一个随机 secret 并持久化到 app_settings（键 secret_key），
保证同一 DB 重启后签发的 JWT 仍可校验，不会因为进程重启就让所有会话失效。
"""
import hashlib
import secrets

import jwt
from jwt import InvalidTokenError  # noqa: F401  # 重导出，调用方 except security.InvalidTokenError
from passlib.context import CryptContext

from kbase.models import AppSetting

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SESSION_TOKEN_TTL_SECONDS = 7 * 24 * 3600
ALGORITHM = "HS256"

API_KEY_PREFIX = "kbase_ak_"
API_KEY_RANDOM_LENGTH = 32

_SECRET_SETTING_KEY = "secret_key"


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _pwd_context.verify(password, password_hash)


def create_session_token(username: str, role: str, *, secret: str) -> str:
    import time
    now = int(time.time())
    payload = {
        "sub": username,
        "role": role,
        "iat": now,
        "exp": now + SESSION_TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_session_token(token: str, *, secret: str) -> dict:
    """校验失败（过期/篡改/密钥不符）统一抛 jwt.InvalidTokenError 及其子类
    （ExpiredSignatureError/InvalidSignatureError 等均是其子类），调用方
    只需 except InvalidTokenError 一处兜底。"""
    return jwt.decode(token, secret, algorithms=[ALGORITHM])


def resolve_secret_key(sf) -> str:
    """env KBASE_SECRET_KEY 优先；否则从 app_settings 读取，不存在则生成
    一个新的随机 secret 并持久化（同一 DB 重启后保持稳定）。"""
    import os
    env_secret = os.environ.get("KBASE_SECRET_KEY")
    if env_secret:
        return env_secret
    with sf() as s:
        row = s.get(AppSetting, _SECRET_SETTING_KEY)
        if row is not None:
            return row.value
        generated = secrets.token_urlsafe(32)
        s.add(AppSetting(key=_SECRET_SETTING_KEY, value=generated))
        s.commit()
        return generated


def generate_api_key() -> tuple[str, str, str]:
    """返回 (完整 key, prefix, key_hash sha256 hex)。完整 key 只在
    创建时返回一次，DB 只存 prefix + key_hash。
    prefix 取随机部分（kbase_ak_ 之后）的前8字符——而不是完整 key 的前8
    字符，因为所有 key 都以字面量 "kbase_ak_" 开头，取完整 key 前8字符会
    对每一把 key 都得到相同的 "kbase_ak" 常量，在列表页起不到任何辨识作用；
    取随机部分的前8字符才能让管理员在设置页凭 prefix 区分不同的 key。"""
    random_part = secrets.token_urlsafe(API_KEY_RANDOM_LENGTH)[:API_KEY_RANDOM_LENGTH]
    full_key = f"{API_KEY_PREFIX}{random_part}"
    prefix = random_part[:8]
    key_hash = hash_api_key(full_key)
    return full_key, prefix, key_hash


def hash_api_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()
