"""首启引导：users 表为空时自动创建 admin 账号。

密码来源：env KBASE_ADMIN_PASSWORD 优先（部署脚本可预设）；否则生成随机
16 位密码并以 WARNING 级别打到日志（README 已醒目提示首启需查日志取密码）——
密码本身从不落库明文，只存 bcrypt 哈希。
"""
import logging
import secrets
import string
import uuid

from kbase.auth import security
from kbase.models import User

logger = logging.getLogger(__name__)

_RANDOM_PASSWORD_LENGTH = 16
_RANDOM_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def _generate_random_password() -> str:
    return "".join(secrets.choice(_RANDOM_PASSWORD_ALPHABET)
                   for _ in range(_RANDOM_PASSWORD_LENGTH))


def ensure_admin(sf) -> None:
    """users 表为空才创建；已有任何用户则视为已引导过，幂等跳过。"""
    import os
    with sf() as s:
        if s.query(User).first() is not None:
            return
        env_password = os.environ.get("KBASE_ADMIN_PASSWORD")
        if env_password:
            password = env_password
        else:
            password = _generate_random_password()
            logger.warning(
                "首启引导：已自动创建管理员账号 admin，随机密码=%s"
                "（请立即登录后妥善保管；也可通过环境变量 KBASE_ADMIN_PASSWORD 预设）",
                password)
        admin = User(id=str(uuid.uuid4()), username="admin",
                     password_hash=security.hash_password(password),
                     role="admin", disabled=False)
        s.add(admin)
        s.commit()
