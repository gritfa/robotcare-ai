"""Chat experience pack: quick actions, conversation resolved flag, per-message feedback.

Revision ID: 20260805_0013
Revises: 20260805_0012
Create Date: 2026-08-05
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260805_0013"
down_revision: str | None = "20260805_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 先可空建列 → 回填空数组 → 再收紧为 NOT NULL，避免存量行卡住 DDL
    op.add_column(
        "conversation_messages",
        sa.Column("quick_actions_json", sa.JSON(), nullable=True),
    )
    op.execute("UPDATE conversation_messages SET quick_actions_json = '[]'::json")
    op.alter_column("conversation_messages", "quick_actions_json", nullable=False)
    op.add_column("conversations", sa.Column("resolved", sa.Boolean(), nullable=True))
    op.create_table(
        "message_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "message_id",
            sa.Integer(),
            sa.ForeignKey("conversation_messages.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("helpful", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=30), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("message_id", name="uq_message_feedback_message_id"),
        sa.CheckConstraint(
            "reason IS NULL OR reason IN "
            "('off_topic', 'unclear_steps', 'wrong_citation', 'wrong_model', 'still_unresolved')",
            name="ck_message_feedback_reason",
        ),
    )


def downgrade() -> None:
    op.drop_table("message_feedback")
    op.drop_column("conversations", "resolved")
    op.drop_column("conversation_messages", "quick_actions_json")
