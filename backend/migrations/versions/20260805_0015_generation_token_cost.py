"""Record token usage and estimated cost per generation call.

Revision ID: 20260805_0015
Revises: 20260805_0014
Create Date: 2026-08-05

问题背景：generation_service 曾丢弃 provider 响应中的 usage 数据，
GenerationRecord 只有 latency_ms，无法准确统计按型号或用户划分的生成成本。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260805_0015"
down_revision: str | None = "20260805_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 全部可空：存量记录没有 usage 数据，用 0 冒充会让成本统计凭空少算
    op.add_column(
        "generation_records", sa.Column("prompt_tokens", sa.Integer(), nullable=True)
    )
    op.add_column(
        "generation_records", sa.Column("completion_tokens", sa.Integer(), nullable=True)
    )
    op.add_column(
        "generation_records",
        # 单位固定为"元"，精度 6 位小数：单次调用常常是 0.0003 元这个量级
        sa.Column("estimated_cost", sa.Numeric(precision=12, scale=6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("generation_records", "estimated_cost")
    op.drop_column("generation_records", "completion_tokens")
    op.drop_column("generation_records", "prompt_tokens")
