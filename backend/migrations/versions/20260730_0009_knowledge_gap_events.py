"""阶段3：内容缺口事件表 knowledge_gap_events（检索空结果 / 资料缺口拒答埋点）。

Revision ID: 20260730_0009
Revises: 20260730_0008
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0009"
down_revision: str | None = "20260730_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_gap_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "robot_model_id", sa.Integer(), sa.ForeignKey("robot_models.id"), nullable=False
        ),
        sa.Column("query_normalized", sa.String(length=2000), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('search_empty', 'answer_knowledge_gap')",
            name="ck_knowledge_gap_events_source",
        ),
    )
    op.create_index(
        "ix_knowledge_gap_events_robot_model_id", "knowledge_gap_events", ["robot_model_id"]
    )
    op.create_index(
        "ix_knowledge_gap_events_created_at", "knowledge_gap_events", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_gap_events_created_at", table_name="knowledge_gap_events")
    op.drop_index("ix_knowledge_gap_events_robot_model_id", table_name="knowledge_gap_events")
    op.drop_table("knowledge_gap_events")
