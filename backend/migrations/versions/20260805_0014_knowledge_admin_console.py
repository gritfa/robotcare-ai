"""Knowledge admin console: document lifecycle, version history, content gap closure.

Revision ID: 20260805_0014
Revises: 20260805_0013
Create Date: 2026-08-05
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260805_0014"
down_revision: str | None = "20260805_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 状态/版本两列模型侧非空：先可空建列 → 回填存量 → 再收紧，
    # 否则 compare_metadata 报 modify_nullable 漂移。
    op.add_column(
        "knowledge_documents",
        sa.Column("status", sa.String(length=20), nullable=True, server_default="active"),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("version", sa.Integer(), nullable=True, server_default="1"),
    )
    op.execute("UPDATE knowledge_documents SET status = 'active' WHERE status IS NULL")
    op.execute("UPDATE knowledge_documents SET version = 1 WHERE version IS NULL")
    op.alter_column("knowledge_documents", "status", nullable=False)
    op.alter_column("knowledge_documents", "version", nullable=False)
    # 存量文档没有原件存档，这些列必须允许为空——不能假装历史数据可回滚。
    op.add_column(
        "knowledge_documents",
        sa.Column("stored_filename", sa.String(length=255), nullable=True),
    )
    op.add_column("knowledge_documents", sa.Column("file_size", sa.Integer(), nullable=True))
    op.add_column(
        "knowledge_documents",
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column(
            "uploaded_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_knowledge_documents_status",
        "knowledge_documents",
        "status IN ('active', 'disabled')",
    )

    op.create_table(
        "knowledge_document_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False, index=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_url", sa.String(length=2000), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("stored_filename", sa.String(length=255), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column("change_kind", sa.String(length=20), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            index=True,
        ),
        sa.UniqueConstraint("document_id", "version"),
        sa.CheckConstraint(
            "change_kind IN ('upload', 'reindex', 'rollback', 'release')",
            name="ck_knowledge_document_versions_change_kind",
        ),
    )

    op.create_table(
        "content_gap_resolutions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "robot_model_id",
            sa.Integer(),
            sa.ForeignKey("robot_models.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("query_normalized", sa.String(length=2000), nullable=False),
        sa.Column("query_hash", sa.String(length=64), nullable=False, index=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "linked_document_id",
            sa.Integer(),
            sa.ForeignKey("knowledge_documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("replay_status", sa.String(length=20), nullable=True),
        sa.Column("replay_answer_excerpt", sa.Text(), nullable=True),
        sa.Column("replay_citation_count", sa.Integer(), nullable=True),
        sa.Column("replay_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("robot_model_id", "query_hash"),
        sa.CheckConstraint(
            "status IN ('open', 'investigating', 'resolved', 'wont_fix')",
            name="ck_content_gap_resolutions_status",
        ),
        sa.CheckConstraint(
            "replay_status IS NULL OR replay_status IN ('passed', 'failed', 'error')",
            name="ck_content_gap_resolutions_replay_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("content_gap_resolutions")
    op.drop_table("knowledge_document_versions")
    op.drop_constraint("ck_knowledge_documents_status", "knowledge_documents", type_="check")
    op.drop_column("knowledge_documents", "uploaded_by")
    op.drop_column("knowledge_documents", "embedding_model")
    op.drop_column("knowledge_documents", "file_size")
    op.drop_column("knowledge_documents", "stored_filename")
    op.drop_column("knowledge_documents", "version")
    op.drop_column("knowledge_documents", "status")
