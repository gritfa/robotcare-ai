"""Let knowledge gap events come from chat, and record why they were refused.

Revision ID: 20260806_0017
Revises: 20260805_0016
Create Date: 2026-08-06

体检 #2：内容缺口埋点此前只挂在 /knowledge/answer 上，而前端对该端点零调用
——实测拒答 8 次、缺口表 0 行、缺口榜 0 条，「缺口闭环」从上线起没产生过一条
数据。埋点下沉到生成层后，聊天（真正的主入口）也会记录，source 需要新值。

同时补 refusal_reason：运营看缺口榜时，"检索完全没命中"和"检索到了但模型说
资料不足"是两种不同的补资料动作，之前一列都区分不出来。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260806_0017"
down_revision: str | None = "20260805_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_SOURCES = "'search_empty', 'answer_knowledge_gap'"
# 旧值保留，不改写既有行：source 是"从哪个入口发现的缺口"，
# answer_knowledge_gap 仍然是 /knowledge/answer 的历史口径。
_NEW_SOURCES = (
    "'search_empty', 'answer_knowledge_gap', 'chat_refusal', 'knowledge_answer_refusal'"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_knowledge_gap_events_source", "knowledge_gap_events", type_="check"
    )
    op.create_check_constraint(
        "ck_knowledge_gap_events_source",
        "knowledge_gap_events",
        f"source IN ({_NEW_SOURCES})",
    )
    op.add_column(
        "knowledge_gap_events",
        sa.Column("refusal_reason", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    # 回退前先把新来源的行归到旧口径，否则约束建不回来
    op.execute(
        "UPDATE knowledge_gap_events SET source = 'answer_knowledge_gap' "
        "WHERE source IN ('chat_refusal', 'knowledge_answer_refusal')"
    )
    op.drop_column("knowledge_gap_events", "refusal_reason")
    op.drop_constraint(
        "ck_knowledge_gap_events_source", "knowledge_gap_events", type_="check"
    )
    op.create_check_constraint(
        "ck_knowledge_gap_events_source",
        "knowledge_gap_events",
        f"source IN ({_OLD_SOURCES})",
    )
