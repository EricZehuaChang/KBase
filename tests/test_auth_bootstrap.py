"""kbase/auth/bootstrap.py 单测：首启建 admin（env 密码优先/随机密码打日志）、幂等。"""
import logging

from kbase.auth import security
from kbase.auth.bootstrap import ensure_admin
from kbase.db import make_session_factory
from kbase.models import User


def test_ensure_admin_creates_admin_when_users_empty(tmp_path):
    sf = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    ensure_admin(sf)
    with sf() as s:
        users = s.query(User).all()
        assert len(users) == 1
        assert users[0].username == "admin"
        assert users[0].role == "admin"
        assert not users[0].disabled


def test_ensure_admin_uses_env_password(tmp_path, monkeypatch):
    monkeypatch.setenv("KBASE_ADMIN_PASSWORD", "my-env-pass")
    sf = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    ensure_admin(sf)
    with sf() as s:
        admin = s.query(User).filter_by(username="admin").first()
        assert security.verify_password("my-env-pass", admin.password_hash)


def test_ensure_admin_generates_random_password_and_logs_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("KBASE_ADMIN_PASSWORD", raising=False)
    sf = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    with caplog.at_level(logging.WARNING):
        ensure_admin(sf)
    assert any("admin" in rec.message.lower() or "密码" in rec.message
               for rec in caplog.records)
    with sf() as s:
        admin = s.query(User).filter_by(username="admin").first()
        assert admin is not None
        assert admin.password_hash    # 有哈希，但密码本身不落库明文


def test_ensure_admin_idempotent_when_users_exist(tmp_path):
    sf = make_session_factory(f"sqlite:///{tmp_path}/kb.sqlite")
    ensure_admin(sf)
    with sf() as s:
        first_count = s.query(User).count()
    ensure_admin(sf)     # 再次调用不应重复创建
    with sf() as s:
        assert s.query(User).count() == first_count
