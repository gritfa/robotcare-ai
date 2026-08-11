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

    池参数必须显式配置：SSE 聊天会在生成期间占用连接，容量不足会影响登录等
    常规请求。CLI 未传入 settings 时沿用默认值，因为它们是短生命周期单连接进程。
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
