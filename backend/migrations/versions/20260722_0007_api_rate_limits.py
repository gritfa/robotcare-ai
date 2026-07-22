"""Add database-backed API and Embedding quotas.

Revision ID: 20260722_0007
Revises: 20260722_0006
Create Date: 2026-07-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0007"
down_revision: str | None = "20260722_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_rate_limits",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("principal_hash", sa.String(length=64), nullable=False),
        sa.Column("window_kind", sa.String(length=20), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope IN ('user', 'email', 'ip')", name="ck_api_rate_limits_scope"
        ),
        sa.CheckConstraint(
            "window_kind IN ('minute', 'day')",
            name="ck_api_rate_limits_window_kind",
        ),
        sa.CheckConstraint(
            "request_count >= 0", name="ck_api_rate_limits_request_count_nonnegative"
        ),
        sa.PrimaryKeyConstraint("key_hash"),
    )
    op.create_index(
        "ix_api_rate_limits_action_scope",
        "api_rate_limits",
        ["action", "scope"],
    )
    op.create_index(
        "ix_api_rate_limits_expires_at", "api_rate_limits", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_api_rate_limits_expires_at", table_name="api_rate_limits")
    op.drop_index("ix_api_rate_limits_action_scope", table_name="api_rate_limits")
    op.drop_table("api_rate_limits")
