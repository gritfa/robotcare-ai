"""Link diagnostic sessions to the chat conversation they were escalated from.

Revision ID: 20260804_0011
Revises: 20260804_0010
Create Date: 2026-08-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260804_0011"
down_revision: str | None = "20260804_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "diagnostic_sessions",
        sa.Column(
            "source_conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversations.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_diagnostic_sessions_source_conversation_id",
        "diagnostic_sessions",
        ["source_conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_diagnostic_sessions_source_conversation_id", table_name="diagnostic_sessions"
    )
    op.drop_column("diagnostic_sessions", "source_conversation_id")
