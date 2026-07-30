from __future__ import annotations

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base
from app.migration_policy import include_migration_object
from app.models import DiagnosticFlow, DiagnosticStep, RobotModel, User
from app.main import create_app
from app.migration_guard import ensure_database_at_head
from app.seed import seed_database
from conftest import alembic_config, fresh_database


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
    "login_throttles": {
        "ck_login_throttles_scope",
        "ck_login_throttles_scope_keys",
    },
}


def assert_state_check_constraints(engine) -> None:
    inspector = inspect(engine)
    for table_name, expected_names in STATE_CHECK_CONSTRAINTS.items():
        actual_names = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table_name)
        }
        assert expected_names <= actual_names


def insert_state_constraint_fixture(engine) -> None:
    with Session(engine) as db:
        seed_database(db)
        flow = db.scalar(
            select(DiagnosticFlow).where(DiagnosticFlow.status == "published")
        )
        assert flow is not None and flow.steps
        model_id = flow.robot_model_id
        flow_id = flow.id
        step_id = flow.steps[0].id

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, email, password_hash, role, status, created_at) VALUES "
                "(9001, 'constraint@example.com', 'hash', 'user', 'active', "
                "'2026-07-22 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO user_devices "
                "(id, user_id, robot_model_id, nickname, created_at) VALUES "
                "(9001, 9001, :model_id, 'constraint device', "
                "'2026-07-22 00:00:00')"
            ),
            {"model_id": model_id},
        )
        connection.execute(
            text(
                "INSERT INTO diagnostic_sessions "
                "(id, user_id, device_id, flow_id, issue_description, status, "
                "current_position, resolved, created_at, updated_at) VALUES "
                "(9001, 9001, 9001, :flow_id, 'fixture', 'in_progress', 1, "
                "NULL, '2026-07-22 00:00:00', '2026-07-22 00:00:00')"
            ),
            {"flow_id": flow_id},
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
                "(id, session_id, step_id, outcome, created_at) VALUES "
                "(9001, 9001, :step_id, 'not_resolved', '2026-07-22 00:00:00')"
            ),
            {"step_id": step_id},
        )


def test_upgrade_head_creates_complete_schema_and_supports_seed(
    tmp_path, migration_database_url
):
    database_url = migration_database_url

    command.upgrade(alembic_config(database_url), "head")

    engine = create_engine(database_url)
    inspector = inspect(engine)
    expected_tables = {
        "alembic_version",
        "api_rate_limits",
        "attachments",
        "auth_sessions",
        "audit_logs",
        "diagnostic_flows",
        "diagnostic_sessions",
        "diagnostic_steps",
        "generation_records",
        "issue_categories",
        "knowledge_chunks",
        "knowledge_documents",
        "login_throttles",
        "pending_file_deletions",
        "refresh_tokens",
        "robot_models",
        "safety_block_events",
        "service_reports",
        "step_executions",
        "user_devices",
        "users",
    }
    assert set(inspector.get_table_names()) == expected_tables

    user_columns = {column["name"] for column in inspector.get_columns("users")}
    assert "status" in user_columns
    user_indexes = {index["name"] for index in inspector.get_indexes("users")}
    assert "ix_users_status" in user_indexes

    auth_session_columns = {
        column["name"] for column in inspector.get_columns("auth_sessions")
    }
    assert {
        "id",
        "user_id",
        "created_at",
        "expires_at",
        "revoked_at",
        "revoke_reason",
    } == auth_session_columns
    refresh_columns = {
        column["name"] for column in inspector.get_columns("refresh_tokens")
    }
    assert {
        "id",
        "session_id",
        "token_hash",
        "created_at",
        "expires_at",
        "used_at",
        "revoked_at",
    } == refresh_columns
    throttle_columns = {
        column["name"] for column in inspector.get_columns("login_throttles")
    }
    assert {
        "key_hash",
        "scope",
        "email_hash",
        "client_ip_hash",
        "failure_count",
        "window_started_at",
        "locked_until",
        "updated_at",
    } == throttle_columns
    throttle_indexes = {
        index["name"] for index in inspector.get_indexes("login_throttles")
    }
    assert "ix_login_throttles_ip_scope" in throttle_indexes
    assert next(
        column for column in inspector.get_columns("login_throttles")
        if column["name"] == "email_hash"
    )["nullable"] is True

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
    assert_state_check_constraints(engine)

    # pgvector schema evidence: the embedding column and its HNSW index come
    # from the explicit migration, not from metadata auto-creation.
    with engine.connect() as connection:
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

    # This comparison is deliberately broader than the spot checks above: it
    # fails whenever a mapped table, column, constraint, index, or type is not
    # represented by the explicit baseline migration.
    with engine.connect() as connection:
        migration_context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "include_object": include_migration_object},
        )
        assert compare_metadata(migration_context, Base.metadata) == []

    with Session(engine) as session:
        seed_database(session)
        assert session.scalar(select(RobotModel).where(RobotModel.code == "JH69U1")) is not None
        assert session.scalar(select(RobotModel).where(RobotModel.code == "VC35U1")) is not None
        flows = session.scalars(select(DiagnosticFlow)).all()
        assert len(flows) == 30  # 5 条真实型号流程 + 25 条 D1 合成演示流程
        assert sum(flow.status == "published" for flow in flows) == 29
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


def test_unversioned_database_is_rejected_by_production_startup(
    migration_database_url,
):
    engine = create_engine(migration_database_url)
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        ensure_database_at_head(engine)
    engine.dispose()


def test_environment_database_url_takes_precedence(
    migration_database_url, monkeypatch
):
    environment_database_url = fresh_database("robotcare_migration_env")
    monkeypatch.setenv("ROBOTCARE_DATABASE_URL", environment_database_url)

    command.upgrade(alembic_config(migration_database_url), "head")

    environment_engine = create_engine(environment_database_url)
    configured_engine = create_engine(migration_database_url)
    assert "alembic_version" in inspect(environment_engine).get_table_names()
    assert inspect(configured_engine).get_table_names() == []
    environment_engine.dispose()
    configured_engine.dispose()


def test_audit_log_migration_upgrades_existing_0001_database_without_data_loss(
    migration_database_url,
):
    database_url = migration_database_url
    config = alembic_config(database_url)
    command.upgrade(config, "20260720_0001")

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (email, password_hash, role, created_at) "
                "VALUES (:email, :password_hash, :role, :created_at)"
            ),
            {
                "email": "preserved@example.com",
                "password_hash": "hash",
                "role": "user",
                "created_at": "2026-07-20 00:00:00",
            },
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    assert "audit_logs" in inspect(engine).get_table_names()
    with Session(engine) as db:
        preserved = db.scalar(select(User).where(User.email == "preserved@example.com"))
        assert preserved is not None
        assert preserved.role == "user"
        assert preserved.status == "active"
    engine.dispose()


def test_auth_migration_upgrades_0002_and_preserves_users_and_audit_logs(
    migration_database_url,
):
    database_url = migration_database_url
    config = alembic_config(database_url)
    command.upgrade(config, "20260720_0002")

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, created_at) "
                "VALUES (1, :email, :password_hash, 'admin', :created_at)"
            ),
            {
                "email": "before-auth-migration@example.com",
                "password_hash": "preserved-hash",
                "created_at": "2026-07-20 00:00:00",
            },
        )
        connection.execute(
            text(
                "INSERT INTO audit_logs "
                "(actor_user_id, action, resource_type, resource_id, details_json, created_at) "
                "VALUES (1, 'preserve', 'user', '1', '{}', :created_at)"
            ),
            {"created_at": "2026-07-20 00:00:01"},
        )
    engine.dispose()

    command.upgrade(config, "20260720_0003")
    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert {"auth_sessions", "refresh_tokens", "login_throttles"} <= set(
        inspector.get_table_names()
    )
    with engine.connect() as connection:
        user_row = connection.execute(
            text(
                "SELECT email, password_hash, role, status FROM users WHERE id = 1"
            )
        ).mappings().one()
        assert dict(user_row) == {
            "email": "before-auth-migration@example.com",
            "password_hash": "preserved-hash",
            "role": "admin",
            "status": "active",
        }
        assert connection.execute(
            text("SELECT count(*) FROM audit_logs WHERE action = 'preserve'")
        ).scalar_one() == 1
    engine.dispose()


def test_state_constraint_migration_upgrades_0004_without_data_loss(
    migration_database_url,
):
    database_url = migration_database_url
    config = alembic_config(database_url)
    command.upgrade(config, "20260720_0004")

    engine = create_engine(database_url)
    insert_state_constraint_fixture(engine)
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    assert_state_check_constraints(engine)
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT role FROM users WHERE id = 9001")
        ) == "user"
        assert connection.scalar(
            text("SELECT status FROM diagnostic_sessions WHERE id = 9001")
        ) == "in_progress"
        assert connection.scalar(
            text("SELECT outcome FROM step_executions WHERE id = 9001")
        ) == "not_resolved"
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "20260730_0008"
        )
    engine.dispose()


def test_state_constraint_migration_rejects_all_invalid_historical_rows_before_ddl(
    migration_database_url,
):
    database_url = migration_database_url
    config = alembic_config(database_url)
    command.upgrade(config, "20260720_0004")

    engine = create_engine(database_url)
    insert_state_constraint_fixture(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE users SET role = 'owner' WHERE id = 9001"))
        connection.execute(
            text(
                "UPDATE diagnostic_flows SET status = 'broken', version = 0 "
                "WHERE id = (SELECT MIN(id) FROM diagnostic_flows)"
            )
        )
        connection.execute(
            text(
                "UPDATE diagnostic_sessions SET status = 'paused', "
                "current_position = -1 WHERE id = 9001"
            )
        )
        connection.execute(
            text("UPDATE step_executions SET outcome = 'skipped' WHERE id = 9001")
        )
        connection.execute(
            text(
                "UPDATE safety_block_events SET risk_level = 'unknown' "
                "WHERE id = 9001"
            )
        )
        connection.execute(
            text(
                "UPDATE diagnostic_steps SET evidence_level = 'guess' "
                "WHERE id = (SELECT MIN(id) FROM diagnostic_steps)"
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError) as exc_info:
        command.upgrade(config, "head")
    message = str(exc_info.value)
    for field in (
        "users.role",
        "diagnostic_flows.status",
        "diagnostic_sessions.status",
        "step_executions.outcome",
        "safety_block_events.risk_level",
        "diagnostic_steps.evidence_level",
        "diagnostic_flows.version",
        "diagnostic_sessions.current_position",
    ):
        assert f"{field}=1" in message

    engine = create_engine(database_url)
    assert connection_revision(engine) == "20260720_0004"
    assert "ck_users_role" not in {
        constraint["name"]
        for constraint in inspect(engine).get_check_constraints("users")
    }
    engine.dispose()


def test_independent_ip_throttle_migration_preserves_existing_rows_and_enforces_keys(
    migration_database_url,
):
    database_url = migration_database_url
    config = alembic_config(database_url)
    command.upgrade(config, "20260722_0005")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO login_throttles "
                "(key_hash, scope, email_hash, client_ip_hash, failure_count, "
                "window_started_at, locked_until, updated_at) VALUES "
                "(:key, 'email', :email, NULL, 2, :created, NULL, :created)"
            ),
            {
                "key": "a" * 64,
                "email": "b" * 64,
                "created": "2026-07-22 00:00:00",
            },
        )
    engine.dispose()


    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        assert connection.scalar(
            text("SELECT failure_count FROM login_throttles WHERE key_hash = :key"),
            {"key": "a" * 64},
        ) == 2
        connection.execute(
            text(
                "INSERT INTO login_throttles "
                "(key_hash, scope, email_hash, client_ip_hash, failure_count, "
                "window_started_at, locked_until, updated_at) VALUES "
                "(:key, 'ip', NULL, :ip, 1, :created, NULL, :created)"
            ),
            {
                "key": "c" * 64,
                "ip": "d" * 64,
                "created": "2026-07-22 00:00:00",
            },
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO login_throttles "
                    "(key_hash, scope, email_hash, client_ip_hash, failure_count, "
                    "window_started_at, locked_until, updated_at) VALUES "
                    "(:key, 'ip', :email, :ip, 1, :created, NULL, :created)"
                ),
                {
                    "key": "e" * 64,
                    "email": "f" * 64,
                    "ip": "1" * 64,
                    "created": "2026-07-22 00:00:00",
                },
            )
    engine.dispose()


def test_api_rate_limit_migration_constraints_and_downgrade(migration_database_url):
    database_url = migration_database_url
    config = alembic_config(database_url)
    command.upgrade(config, "20260722_0006")
    engine = create_engine(database_url)
    assert "api_rate_limits" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert_state_check_constraints(engine)
    assert {"ix_api_rate_limits_action_scope", "ix_api_rate_limits_expires_at"} <= {
        index["name"] for index in inspector.get_indexes("api_rate_limits")
    }
    valid_values = {
        "key": "a" * 64,
        "action": "knowledge_search",
        "scope": "user",
        "principal": "b" * 64,
        "window": "minute",
        "started": "2026-07-22 00:00:00",
        "expires": "2026-07-22 00:01:00",
        "count": 1,
    }
    insert_sql = text(
        "INSERT INTO api_rate_limits "
        "(key_hash, action, scope, principal_hash, window_kind, "
        "window_started_at, expires_at, request_count, updated_at) VALUES "
        "(:key, :action, :scope, :principal, :window, :started, :expires, "
        ":count, :started)"
    )
    with engine.begin() as connection:
        connection.execute(insert_sql, valid_values)
    for suffix, overrides in (
        ("c", {"scope": "device"}),
        ("d", {"window": "hour"}),
        ("e", {"count": -1}),
    ):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    insert_sql,
                    {**valid_values, **overrides, "key": suffix * 64},
                )
    engine.dispose()

    command.downgrade(config, "20260722_0006")
    engine = create_engine(database_url)
    assert "api_rate_limits" not in inspect(engine).get_table_names()
    assert "login_throttles" in inspect(engine).get_table_names()
    assert connection_revision(engine) == "20260722_0006"
    engine.dispose()


def test_independent_ip_throttle_migration_rejects_invalid_legacy_scope_keys(
    migration_database_url,
):
    database_url = migration_database_url
    config = alembic_config(database_url)
    command.upgrade(config, "20260722_0005")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO login_throttles "
                "(key_hash, scope, email_hash, client_ip_hash, failure_count, "
                "window_started_at, locked_until, updated_at) VALUES "
                "(:key, 'email', :email, :ip, 1, :created, NULL, :created)"
            ),
            {
                "key": "9" * 64,
                "email": "8" * 64,
                "ip": "7" * 64,
                "created": "2026-07-22 00:00:00",
            },
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="invalid legacy scope/key"):
        command.upgrade(config, "head")

    engine = create_engine(database_url)
    assert connection_revision(engine) == "20260722_0005"
    engine.dispose()


def test_independent_ip_throttle_downgrade_refuses_to_delete_ip_history(
    migration_database_url,
):
    database_url = migration_database_url
    config = alembic_config(database_url)
    command.upgrade(config, "20260722_0006")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO login_throttles "
                "(key_hash, scope, email_hash, client_ip_hash, failure_count, "
                "window_started_at, locked_until, updated_at) VALUES "
                "(:key, 'ip', NULL, :ip, 1, :created, NULL, :created)"
            ),
            {
                "key": "6" * 64,
                "ip": "5" * 64,
                "created": "2026-07-22 00:00:00",
            },
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="refusing destructive downgrade"):
        command.downgrade(config, "20260722_0005")

    engine = create_engine(database_url)
    assert connection_revision(engine) == "20260722_0006"
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT COUNT(*) FROM login_throttles WHERE scope = 'ip'")
        ) == 1
    engine.dispose()


def connection_revision(engine) -> str:
    with engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version"))
