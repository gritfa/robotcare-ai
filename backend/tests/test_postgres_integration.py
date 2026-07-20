from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, select, text

from app.database import Base, build_session_factory
from app.knowledge_service import search_knowledge
from app.migration_policy import include_migration_object
from app.models import KnowledgeChunk, KnowledgeDocument, RobotModel
from app.seed import seed_database


POSTGRES_URL = os.getenv("ROBOTCARE_TEST_POSTGRES_URL")
ALLOW_DESTRUCTIVE = os.getenv("ROBOTCARE_ALLOW_DESTRUCTIVE_POSTGRES_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL or not ALLOW_DESTRUCTIVE,
    reason="Set a dedicated ROBOTCARE_TEST_POSTGRES_URL and destructive-test opt-in",
)
BACKEND_ROOT = Path(__file__).resolve().parents[1]


def vector(first: float, second: float = 0.0) -> list[float]:
    return [first, second, *([0.0] * 254)]


class FakeProvider:
    def embed_query(self, text):
        return vector(1.0)


def config() -> Config:
    value = Config(str(BACKEND_ROOT / "alembic.ini"))
    value.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    value.set_main_option("sqlalchemy.url", POSTGRES_URL or "")
    return value


def test_postgres_vector_schema_index_model_filter_and_top_k(monkeypatch):
    assert POSTGRES_URL is not None
    assert "robotcare_test" in POSTGRES_URL, "Integration tests require a dedicated test database"
    monkeypatch.setenv("ROBOTCARE_DATABASE_URL", POSTGRES_URL)
    command.downgrade(config(), "base")
    command.upgrade(config(), "head")

    engine = create_engine(POSTGRES_URL)
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')")
        ) is True
        migration_context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "include_object": include_migration_object},
        )
        assert compare_metadata(migration_context, Base.metadata) == []
        assert connection.scalar(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
                "WHERE c.relname='knowledge_chunks' AND a.attname='embedding'"
            )
        ) == "vector(256)"
        assert connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                "WHERE indexname='ix_knowledge_chunks_embedding_hnsw')"
            )
        ) is True
    engine.dispose()

    session_factory = build_session_factory(POSTGRES_URL)
    with session_factory() as db:
        seed_database(db)
        jh = db.scalar(select(RobotModel).where(RobotModel.code == "JH69U1"))
        vc = db.scalar(select(RobotModel).where(RobotModel.code == "VC35U1"))
        assert jh is not None and vc is not None
        jh_doc = KnowledgeDocument(
            robot_model_id=jh.id,
            title="JH official",
            source_url="https://example.com/jh.pdf",
            sha256="a" * 64,
            page_count=1,
        )
        vc_doc = KnowledgeDocument(
            robot_model_id=vc.id,
            title="VC official",
            source_url="https://example.com/vc.pdf",
            sha256="b" * 64,
            page_count=1,
        )
        db.add_all([jh_doc, vc_doc])
        db.flush()
        db.add_all(
            [
                KnowledgeChunk(
                    document_id=jh_doc.id,
                    chunk_index=0,
                    page_number=1,
                    content="best JH result",
                    embedding=vector(1.0),
                ),
                KnowledgeChunk(
                    document_id=jh_doc.id,
                    chunk_index=1,
                    page_number=1,
                    content="second JH result",
                    embedding=vector(0.8, 0.2),
                ),
                KnowledgeChunk(
                    document_id=vc_doc.id,
                    chunk_index=0,
                    page_number=1,
                    content="must not leak from VC",
                    embedding=vector(1.0),
                ),
            ]
        )
        db.commit()

        results = search_knowledge(
            db,
            robot_model_id=jh.id,
            query="charging",
            top_k=1,
            min_score=0.25,
            provider=FakeProvider(),
        )
        assert [item.content for item in results] == ["best JH result"]
        assert all(item.document_title == "JH official" for item in results)
    session_factory.kw["bind"].dispose()
