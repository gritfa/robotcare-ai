"""Record the chat routing decision (intent / rule / action) on assistant messages.

路由层上线前的历史消息三列均为 NULL，前端按普通回答渲染，不需要回填。

Revision ID: 20260805_0012
Revises: 20260804_0011
Create Date: 2026-08-05
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260805_0012"
down_revision: str | None = "20260804_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_messages", sa.Column("intent", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "conversation_messages", sa.Column("routing_rule", sa.String(length=60), nullable=True)
    )
    op.add_column(
        "conversation_messages", sa.Column("action_code", sa.String(length=40), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("conversation_messages", "action_code")
    op.drop_column("conversation_messages", "routing_rule")
    op.drop_column("conversation_messages", "intent")
