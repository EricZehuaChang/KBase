from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kbase.migrations import run_migrations
from kbase.models import Base


def make_session_factory(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    run_migrations(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
