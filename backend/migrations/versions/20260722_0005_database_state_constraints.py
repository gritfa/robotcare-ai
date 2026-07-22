"""Enforce database-level diagnostic state constraints.

Revision ID: 20260722_0005
Revises: 20260720_0004
Create Date: 2026-07-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0005"
down_revision: str | None = "20260720_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INVALID_DATA_CHECKS = (
    ("users.role", "SELECT COUNT(*) FROM users WHERE role NOT IN ('user', 'admin')"),
    (
        "diagnostic_flows.status",
        "SELECT COUNT(*) FROM diagnostic_flows "
        "WHERE status NOT IN ('draft', 'published', 'retired')",
    ),
    (
        "diagnostic_sessions.status",
        "SELECT COUNT(*) FROM diagnostic_sessions "
        "WHERE status NOT IN ('in_progress', 'resolved', 'unresolved')",
    ),
    (
        "step_executions.outcome",
        "SELECT COUNT(*) FROM step_executions "
        "WHERE outcome NOT IN ('resolved', 'not_resolved')",
    ),
    (
        "safety_block_events.risk_level",
        "SELECT COUNT(*) FROM safety_block_events WHERE risk_level <> 'critical'",
    ),
    (
        "diagnostic_steps.evidence_level",
        "SELECT COUNT(*) FROM diagnostic_steps "
        "WHERE evidence_level NOT IN ('direct', 'partial', 'none')",
    ),
    (
        "diagnostic_flows.version",
        "SELECT COUNT(*) FROM diagnostic_flows WHERE version <= 0",
    ),
    (
        "diagnostic_sessions.current_position",
        "SELECT COUNT(*) FROM diagnostic_sessions WHERE current_position < 0",
    ),
)


def _reject_invalid_history() -> None:
    bind = op.get_bind()
    violations: list[str] = []
    for field, statement in INVALID_DATA_CHECKS:
        count = bind.scalar(sa.text(statement))
        if count:
            violations.append(f"{field}={count}")
    if violations:
        joined = "; ".join(violations)
        raise RuntimeError(
            "Cannot add database state constraints because invalid historical "
            f"rows exist: {joined}. Correct the data explicitly before retrying."
        )


def upgrade() -> None:
    # Validate every target before the first DDL statement. This keeps a failed
    # SQLite batch migration from leaving a partially upgraded schema.
    _reject_invalid_history()

    with op.batch_alter_table("users") as batch_op:
        batch_op.create_check_constraint(
            "ck_users_role", "role IN ('user', 'admin')"
        )

    with op.batch_alter_table("diagnostic_flows") as batch_op:
        batch_op.create_check_constraint(
            "ck_diagnostic_flows_status",
            "status IN ('draft', 'published', 'retired')",
        )
        batch_op.create_check_constraint(
            "ck_diagnostic_flows_version_positive", "version > 0"
        )

    with op.batch_alter_table("diagnostic_steps") as batch_op:
        batch_op.create_check_constraint(
            "ck_diagnostic_steps_evidence_level",
            "evidence_level IN ('direct', 'partial', 'none')",
        )

    with op.batch_alter_table("diagnostic_sessions") as batch_op:
        batch_op.create_check_constraint(
            "ck_diagnostic_sessions_status",
            "status IN ('in_progress', 'resolved', 'unresolved')",
        )
        batch_op.create_check_constraint(
            "ck_diagnostic_sessions_current_position_nonnegative",
            "current_position IS NULL OR current_position >= 0",
        )

    with op.batch_alter_table("safety_block_events") as batch_op:
        batch_op.create_check_constraint(
            "ck_safety_block_events_risk_level", "risk_level = 'critical'"
        )

    with op.batch_alter_table("step_executions") as batch_op:
        batch_op.create_check_constraint(
            "ck_step_executions_outcome",
            "outcome IN ('resolved', 'not_resolved')",
        )


def downgrade() -> None:
    with op.batch_alter_table("step_executions") as batch_op:
        batch_op.drop_constraint("ck_step_executions_outcome", type_="check")

    with op.batch_alter_table("safety_block_events") as batch_op:
        batch_op.drop_constraint("ck_safety_block_events_risk_level", type_="check")

    with op.batch_alter_table("diagnostic_sessions") as batch_op:
        batch_op.drop_constraint(
            "ck_diagnostic_sessions_current_position_nonnegative", type_="check"
        )
        batch_op.drop_constraint("ck_diagnostic_sessions_status", type_="check")

    with op.batch_alter_table("diagnostic_steps") as batch_op:
        batch_op.drop_constraint(
            "ck_diagnostic_steps_evidence_level", type_="check"
        )

    with op.batch_alter_table("diagnostic_flows") as batch_op:
        batch_op.drop_constraint(
            "ck_diagnostic_flows_version_positive", type_="check"
        )
        batch_op.drop_constraint("ck_diagnostic_flows_status", type_="check")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("ck_users_role", type_="check")
