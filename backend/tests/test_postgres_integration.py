from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.database import Base, build_session_factory
from app.config import Settings
from app.knowledge_service import search_knowledge
from app.migration_policy import include_migration_object
from app.models import ApiRateLimit, KnowledgeChunk, KnowledgeDocument, RobotModel
from app.rate_limit_service import RateQuota, consume_rate_quotas
from app.seed import seed_database


POSTGRES_URL = os.getenv("ROBOTCARE_TEST_POSTGRES_URL")
ALLOW_DESTRUCTIVE = os.getenv("ROBOTCARE_ALLOW_DESTRUCTIVE_POSTGRES_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL or not ALLOW_DESTRUCTIVE,
    reason="Set a dedicated ROBOTCARE_TEST_POSTGRES_URL and destructive-test opt-in",
)
BACKEND_ROOT = Path(__file__).resolve().parents[1]

STATE_CHECK_CONSTRAINTS = {
    "api_rate_limits": {
        "ck_api_rate_limits_scope",
        "ck_api_rate_limits_window_kind",
        "ck_api_rate_limits_request_count_nonnegative",
    },
    "users": {"ck_users_role"},
    "diagnostic_flows": {
        "ck_diagnostic_flows_status",
        "ck_diagnostic_flows_version_positive",
    },
    "diagnostic_sessions": {
        "ck_diagnostic_sessions_status",
        "ck_diagnostic_sessions_current_position_nonnegative",
    },
    "step_executions": {"ck_step_executions_outcome"},
    "safety_block_events": {"ck_safety_block_events_risk_level"},
    "diagnostic_steps": {"ck_diagnostic_steps_evidence_level"},
}


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
    command.upgrade(config(), "20260720_0004")
    pre_constraint_factory = build_session_factory(POSTGRES_URL)
    with pre_constraint_factory() as db:
        seed_database(db)
    pre_constraint_factory.kw["bind"].dispose()
    command.upgrade(config(), "head")

    engine = create_engine(POSTGRES_URL)
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')")
        ) is True
        inspector = inspect(connection)
        for table_name, expected_names in STATE_CHECK_CONSTRAINTS.items():
            actual_names = {
                constraint["name"]
                for constraint in inspector.get_check_constraints(table_name)
            }
            assert expected_names <= actual_names
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

    engine = session_factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, email, password_hash, role, status, created_at) VALUES "
                "(9001, 'pg-constraint@example.com', 'hash', 'user', 'active', "
                "'2026-07-22 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO user_devices "
                "(id, user_id, robot_model_id, nickname, created_at) "
                "SELECT 9001, 9001, id, 'pg constraint device', "
                "'2026-07-22 00:00:00' FROM robot_models WHERE code = 'JH69U1'"
            )
        )
        connection.execute(
            text(
                "INSERT INTO diagnostic_sessions "
                "(id, user_id, device_id, flow_id, issue_description, status, "
                "current_position, resolved, created_at, updated_at) "
                "SELECT 9001, 9001, 9001, id, 'fixture', 'in_progress', 1, NULL, "
                "'2026-07-22 00:00:00', '2026-07-22 00:00:00' "
                "FROM diagnostic_flows ORDER BY id LIMIT 1"
            )
        )
        connection.execute(
            text(
                "INSERT INTO safety_block_events "
                "(id, user_id, device_id, category, risk_level, reason, advice, "
                "description_sha256, created_at) VALUES "
                "(9001, 9001, 9001, 'battery', 'critical', 'reason', 'advice', "
                ":sha, '2026-07-22 00:00:00')"
            ),
            {"sha": "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO step_executions "
                "(id, session_id, step_id, outcome, created_at) "
                "SELECT 9001, 9001, id, 'not_resolved', '2026-07-22 00:00:00' "
                "FROM diagnostic_steps ORDER BY id LIMIT 1"
            )
        )

    invalid_statements = (
        "UPDATE users SET role = 'owner' WHERE id = 9001",
        "UPDATE diagnostic_flows SET status = 'broken' "
        "WHERE id = (SELECT MIN(id) FROM diagnostic_flows)",
        "UPDATE diagnostic_flows SET version = 0 "
        "WHERE id = (SELECT MIN(id) FROM diagnostic_flows)",
        "UPDATE diagnostic_sessions SET status = 'paused' WHERE id = 9001",
        "UPDATE diagnostic_sessions SET current_position = -1 WHERE id = 9001",
        "UPDATE step_executions SET outcome = 'skipped' WHERE id = 9001",
        "UPDATE safety_block_events SET risk_level = 'unknown' WHERE id = 9001",
        "UPDATE diagnostic_steps SET evidence_level = 'guess' "
        "WHERE id = (SELECT MIN(id) FROM diagnostic_steps)",
    )
    for statement in invalid_statements:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(statement))

    settings = Settings(_env_file=None)
    quota = RateQuota("postgres_concurrent", "ip", "203.0.113.9", "minute", 5)
    quota_now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

    def consume_once(_index: int) -> int:
        with session_factory() as db:
            try:
                consume_rate_quotas(db, (quota,), settings, now=quota_now)
            except HTTPException as exc:
                return exc.status_code
            return 200

    with ThreadPoolExecutor(max_workers=8) as executor:
        quota_statuses = list(executor.map(consume_once, range(8)))
    assert quota_statuses.count(200) == 5
    assert quota_statuses.count(429) == 3
    with session_factory() as db:
        quota_row = db.scalar(
            select(ApiRateLimit).where(ApiRateLimit.action == "postgres_concurrent")
        )
        assert quota_row is not None and quota_row.request_count == 5
    engine.dispose()
