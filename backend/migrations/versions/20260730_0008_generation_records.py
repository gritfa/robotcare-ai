"""阶段1：生成层调用留痕表 generation_records。

Revision ID: 20260730_0008
Revises: 20260722_0007
Create Date: 2026-07-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0008"
down_revision: str | None = "20260722_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generation_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "robot_model_id", sa.Integer(), sa.ForeignKey("robot_models.id"), nullable=False
        ),
        sa.Column("query", sa.String(length=2000), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("provider_model", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("citations_json", sa.JSON(), nullable=False),
        sa.Column("refusal_reason", sa.String(length=40), nullable=True),
        sa.Column("snippets_sha256", sa.String(length=64), nullable=False),
        sa.Column("snippet_count", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('answered', 'refused')", name="ck_generation_records_status"
        ),
        sa.CheckConstraint(
            "refusal_reason IN ('knowledge_gap', 'model_refused', 'citation_invalid', "
            "'unsafe_answer') OR refusal_reason IS NULL",
            name="ck_generation_records_refusal_reason",
        ),
    )
    op.create_index("ix_generation_records_user_id", "generation_records", ["user_id"])
    op.create_index(
        "ix_generation_records_robot_model_id", "generation_records", ["robot_model_id"]
    )
    op.create_index("ix_generation_records_status", "generation_records", ["status"])


def downgrade() -> None:
    op.drop_index("ix_generation_records_status", table_name="generation_records")
    op.drop_index("ix_generation_records_robot_model_id", table_name="generation_records")
    op.drop_index("ix_generation_records_user_id", table_name="generation_records")
    op.drop_table("generation_records")
