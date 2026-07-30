"""离线评测专用 PostgreSQL 库（robotcare_eval）。

评测脚本不再使用内存数据库：与产品运行时保持同一方言（PostgreSQL + pgvector），
在测试服务器上按需创建 robotcare_eval 库，每次运行前清空重建 schema，
保证评测彼此独立且可重复。
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.database import Base, build_session_factory

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://postgres:test@127.0.0.1:55433/robotcare_test"
)
EVAL_DATABASE_NAME = "robotcare_eval"


def fresh_eval_session_factory() -> sessionmaker[Session]:
    """Create (if needed) and wipe the evaluation database, then return a session factory."""

    base = make_url(
        os.getenv("ROBOTCARE_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    )
    admin = create_engine(base, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        exists = connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": EVAL_DATABASE_NAME},
        )
        if not exists:
            connection.execute(text(f'CREATE DATABASE "{EVAL_DATABASE_NAME}"'))
    admin.dispose()

    eval_url = base.set(database=EVAL_DATABASE_NAME).render_as_string(
        hide_password=False
    )
    plain = create_engine(eval_url, poolclass=NullPool)
    with plain.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    plain.dispose()

    factory = build_session_factory(eval_url)
    Base.metadata.create_all(factory.kw["bind"])
    return factory
