from collections.abc import Generator
from typing import TYPE_CHECKING

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

if TYPE_CHECKING:
    from .config import Settings


class Base(DeclarativeBase):
    pass


def build_session_factory(
    database_url: str, settings: "Settings | None" = None
) -> sessionmaker[Session]:
    """Build the PostgreSQL session factory; pgvector is the only vector type.

    池参数必须显式给：SQLAlchemy 默认 5+10 条在「SSE 聊天占住连接」的形态下
    15 路并发就会把池抽干，连登录一起挂（2026-08-06 体检）。CLI 侧不传 settings
    时沿用默认值即可——它们是短命单连接进程。
    """

    from .config import get_settings

    resolved = settings or get_settings()
    engine = create_engine(
        database_url,
        pool_size=resolved.db_pool_size,
        max_overflow=resolved.db_max_overflow,
        pool_timeout=resolved.db_pool_timeout_seconds,
        pool_recycle=resolved.db_pool_recycle_seconds,
        pool_pre_ping=resolved.db_pool_pre_ping,
    )
    from pgvector.psycopg import register_vector

    @event.listens_for(engine, "connect")
    def register_pgvector(dbapi_connection, connection_record) -> None:
        del connection_record
        register_vector(dbapi_connection)

    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db(request: Request) -> Generator[Session, None, None]:
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
