from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import DiagnosticFlow, DiagnosticStep, RobotModel, User
from app.main import create_app
from app.migration_guard import ensure_database_at_head
from app.seed import seed_database


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_upgrade_head_creates_complete_schema_and_supports_seed(tmp_path, monkeypatch):
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.delenv("ROBOTCARE_DATABASE_URL", raising=False)

    command.upgrade(alembic_config(database_url), "head")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    expected_tables = {
        "alembic_version",
        "attachments",
        "audit_logs",
        "diagnostic_flows",
        "diagnostic_sessions",
        "diagnostic_steps",
        "issue_categories",
        "knowledge_chunks",
        "knowledge_documents",
        "pending_file_deletions",
        "robot_models",
        "safety_block_events",
        "service_reports",
        "step_executions",
        "user_devices",
        "users",
    }
    assert set(inspector.get_table_names()) == expected_tables

    audit_columns = {column["name"] for column in inspector.get_columns("audit_logs")}
    assert {
        "actor_user_id",
        "action",
        "resource_type",
        "resource_id",
        "details_json",
        "created_at",
    } <= audit_columns
    audit_indexes = {index["name"] for index in inspector.get_indexes("audit_logs")}
    assert {
        "ix_audit_logs_action",
        "ix_audit_logs_actor_user_id",
        "ix_audit_logs_created_at",
        "ix_audit_logs_resource",
    } <= audit_indexes

    flow_columns = {column["name"] for column in inspector.get_columns("diagnostic_flows")}
    assert {
        "stable_key",
        "version",
        "status",
        "content_sha256",
        "reviewed_at",
        "reviewed_by",
    } <= flow_columns

    flow_unique_columns = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("diagnostic_flows")
    }
    assert ("stable_key", "version") in flow_unique_columns
    assert ("robot_model_id", "issue_category_id", "version") in flow_unique_columns

    step_unique_columns = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("diagnostic_steps")
    }
    assert ("flow_id", "position") in step_unique_columns
    assert ("flow_id", "stable_key") in step_unique_columns
    step_columns = {column["name"] for column in inspector.get_columns("diagnostic_steps")}
    assert {"evidence_level", "evidence_basis", "policy_note"} <= step_columns

    flow_indexes = {index["name"] for index in inspector.get_indexes("diagnostic_flows")}
    assert {"ix_diagnostic_flows_stable_key", "ix_diagnostic_flows_status"} <= flow_indexes

    # This comparison is deliberately broader than the spot checks above: it
    # fails whenever a mapped table, column, constraint, index, or type is not
    # represented by the explicit baseline migration.
    with engine.connect() as connection:
        migration_context = MigrationContext.configure(connection)
        assert compare_metadata(migration_context, Base.metadata) == []

    with Session(engine) as session:
        seed_database(session)
        assert session.scalar(select(RobotModel).where(RobotModel.code == "JH69U1")) is not None
        assert session.scalar(select(RobotModel).where(RobotModel.code == "VC35U1")) is not None
        flows = session.scalars(select(DiagnosticFlow)).all()
        assert len(flows) == 5
        assert sum(flow.status == "published" for flow in flows) == 4
        assert sum(flow.status == "draft" for flow in flows) == 1
        assert len(session.scalars(select(DiagnosticStep)).all()) > 0

    app = create_app(
        database_url,
        attachment_dir=tmp_path / "attachments",
        report_dir=tmp_path / "reports",
        auto_create_schema=False,
    )
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

    engine.dispose()


def test_unversioned_database_is_rejected_by_production_startup(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'unversioned.db').as_posix()}"
    engine = create_engine(database_url)
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        ensure_database_at_head(engine)
    engine.dispose()


def test_environment_database_url_takes_precedence(tmp_path, monkeypatch):
    environment_database = tmp_path / "from-environment.db"
    configured_database = tmp_path / "from-config.db"
    monkeypatch.setenv(
        "ROBOTCARE_DATABASE_URL", f"sqlite:///{environment_database.as_posix()}"
    )

    command.upgrade(
        alembic_config(f"sqlite:///{configured_database.as_posix()}"),
        "head",
    )

    assert environment_database.exists()
    assert "alembic_version" in inspect(
        create_engine(f"sqlite:///{environment_database.as_posix()}")
    ).get_table_names()
    assert not configured_database.exists()


def test_audit_log_migration_upgrades_existing_0001_database_without_data_loss(
    tmp_path, monkeypatch
):
    database_url = f"sqlite:///{(tmp_path / 'upgrade-0002.db').as_posix()}"
    monkeypatch.delenv("ROBOTCARE_DATABASE_URL", raising=False)
    config = alembic_config(database_url)
    command.upgrade(config, "20260720_0001")

    engine = create_engine(database_url)
    with Session(engine) as db:
        db.add(User(email="preserved@example.com", password_hash="hash", role="user"))
        db.commit()
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    assert "audit_logs" in inspect(engine).get_table_names()
    with Session(engine) as db:
        preserved = db.scalar(select(User).where(User.email == "preserved@example.com"))
        assert preserved is not None
        assert preserved.role == "user"
    engine.dispose()
