"""Add encrypted administrator-managed AI provider configuration.

Revision ID: 20260824_0018
Revises: 20260806_0017
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260824_0018"
down_revision: str | None = "20260806_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_provider_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("llm_backend", sa.String(length=30), nullable=False),
        sa.Column("generation_model", sa.String(length=100), nullable=False),
        sa.Column("generation_base_url", sa.String(length=2000), nullable=True),
        sa.Column("embedding_base_url", sa.String(length=2000), nullable=True),
        sa.Column("generation_api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("embedding_api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_ai_provider_configs_singleton"),
        sa.CheckConstraint(
            "llm_backend IN ('dashscope', 'openai-compat')",
            name="ck_ai_provider_configs_backend",
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("ai_provider_configs")
