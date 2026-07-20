from collections.abc import Generator
from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def build_session_factory(database_url: str) -> sessionmaker[Session]:
    if database_url.startswith("sqlite") and "///" in database_url:
        path = database_url.split("///", 1)[1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    if database_url.startswith("postgresql"):
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
