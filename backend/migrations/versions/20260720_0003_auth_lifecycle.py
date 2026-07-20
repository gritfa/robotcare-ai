"""Add database-backed authentication sessions, refresh rotation and login throttles.

Revision ID: 20260720_0003
Revises: 20260720_0002
Create Date: 2026-07-20
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0003"
down_revision: str | None = "20260720_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="active",
            )
        )
        batch_op.create_check_constraint(
            "ck_users_status", "status IN ('active', 'disabled')"
        )
        batch_op.create_index("ix_users_status", ["status"])

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=40), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_index("ix_auth_sessions_revoked_at", "auth_sessions", ["revoked_at"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"], ["auth_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_refresh_tokens_session_id", "refresh_tokens", ["session_id"]
    )
    op.create_index(
        "ix_refresh_tokens_token_hash",
        "refresh_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"]
    )

    op.create_table(
        "login_throttles",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("email_hash", sa.String(length=64), nullable=False),
        sa.Column("client_ip_hash", sa.String(length=64), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope IN ('email', 'email_ip')", name="ck_login_throttles_scope"
        ),
        sa.PrimaryKeyConstraint("key_hash"),
    )
    op.create_index(
        "ix_login_throttles_email_scope",
        "login_throttles",
        ["email_hash", "scope"],
    )
    op.create_index(
        "ix_login_throttles_locked_until", "login_throttles", ["locked_until"]
    )


def downgrade() -> None:
    op.drop_index("ix_login_throttles_locked_until", table_name="login_throttles")
    op.drop_index("ix_login_throttles_email_scope", table_name="login_throttles")
    op.drop_table("login_throttles")

    op.drop_index("ix_refresh_tokens_expires_at", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_token_hash", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_session_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")

    op.drop_index("ix_auth_sessions_revoked_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_index("ix_users_status")
        batch_op.drop_constraint("ck_users_status", type_="check")
        batch_op.drop_column("status")
