from collections.abc import Generator

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def build_session_factory(database_url: str) -> sessionmaker[Session]:
    """Build the PostgreSQL session factory; pgvector is the only vector type."""

    engine = create_engine(database_url)
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
