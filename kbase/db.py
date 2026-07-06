from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kbase.migrations import run_migrations
from kbase.models import Base

# M4-2 H6 压测调优：SQLAlchemy 对非 SQLite 方言默认 pool_size=5+max_overflow=10
# （合计 15 个并发连接上限），100 并发压测下检索请求（每次 retrieve() 内有
# 多次同步 PG 往返：keyword 查询 + _leaf_texts + _assemble 逐块 get）明显
# 超过这个并发连接数，请求排队等连接是压测中确认的延迟大头（TEI 侧单次
# rerank/embed 调用本身仍在数十到一百多毫秒量级，见 loadtest/report-standard.md
# 的 TEI 日志证据）。SQLite（lite profile）走单文件连接，不适用/不需要这组
# 参数，保持原样只在非 SQLite 分支应用。
_PG_POOL_SIZE = 50
_PG_MAX_OVERFLOW = 50


def make_session_factory(url: str):
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        engine = create_engine(url, connect_args=connect_args)
    else:
        engine = create_engine(url, pool_size=_PG_POOL_SIZE,
                               max_overflow=_PG_MAX_OVERFLOW, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    run_migrations(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
