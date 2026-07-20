"""Create the complete RobotCare AI baseline schema.

Revision ID: 20260720_0001
Revises:
Create Date: 2026-07-20
"""

from collections.abc import Sequence

from alembic import op
from pgvector.sqlalchemy import VECTOR
import sqlalchemy as sa


revision: str = "20260720_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    is_postgresql = op.get_bind().dialect.name == "postgresql"
    if is_postgresql:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "issue_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_issue_categories_code", "issue_categories", ["code"], unique=True)

    op.create_table(
        "pending_file_deletions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("original_stored_filename", sa.String(length=255), nullable=False),
        sa.Column("quarantined_filename", sa.String(length=255), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("quarantined_filename"),
    )

    op.create_table(
        "robot_models",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("brand", sa.String(length=50), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_robot_models_code", "robot_models", ["code"], unique=True)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "diagnostic_flows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stable_key", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("robot_model_id", sa.Integer(), nullable=False),
        sa.Column("issue_category_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=150), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(length=120), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["issue_category_id"], ["issue_categories.id"]),
        sa.ForeignKeyConstraint(["robot_model_id"], ["robot_models.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stable_key", "version"),
        sa.UniqueConstraint("robot_model_id", "issue_category_id", "version"),
    )
    op.create_index("ix_diagnostic_flows_stable_key", "diagnostic_flows", ["stable_key"])
    op.create_index("ix_diagnostic_flows_status", "diagnostic_flows", ["status"])

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("robot_model_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_url", sa.String(length=2000), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["robot_model_id"], ["robot_models.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("robot_model_id", "source_url"),
    )
    op.create_index(
        "ix_knowledge_documents_robot_model_id", "knowledge_documents", ["robot_model_id"]
    )
    op.create_index("ix_knowledge_documents_sha256", "knowledge_documents", ["sha256"])

    op.create_table(
        "user_devices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("robot_model_id", sa.Integer(), nullable=False),
        sa.Column("nickname", sa.String(length=100), nullable=False),
        sa.Column("serial_number", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["robot_model_id"], ["robot_models.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_devices_user_id", "user_devices", ["user_id"])

    op.create_table(
        "diagnostic_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("flow_id", sa.Integer(), nullable=False),
        sa.Column("issue_description", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("current_position", sa.Integer(), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["user_devices.id"]),
        sa.ForeignKeyConstraint(["flow_id"], ["diagnostic_flows.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_diagnostic_sessions_device_id", "diagnostic_sessions", ["device_id"])
    op.create_index("ix_diagnostic_sessions_user_id", "diagnostic_sessions", ["user_id"])

    op.create_table(
        "diagnostic_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("flow_id", sa.Integer(), nullable=False),
        sa.Column("stable_key", sa.String(length=120), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=150), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("source_label", sa.String(length=200), nullable=False),
        sa.Column("source_url", sa.String(length=2000), nullable=False),
        sa.Column("source_page", sa.Integer(), nullable=False),
        sa.Column("evidence_level", sa.String(length=20), nullable=False),
        sa.Column("evidence_basis", sa.Text(), nullable=False),
        sa.Column("policy_note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["flow_id"], ["diagnostic_flows.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("flow_id", "position"),
        sa.UniqueConstraint("flow_id", "stable_key"),
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", VECTOR(256) if is_postgresql else sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "chunk_index"),
    )
    op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])
    if is_postgresql:
        op.execute(
            "CREATE INDEX ix_knowledge_chunks_embedding_hnsw "
            "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )

    op.create_table(
        "safety_block_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column("risk_level", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("advice", sa.String(length=500), nullable=False),
        sa.Column("description_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["user_devices.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_safety_block_events_category", "safety_block_events", ["category"])
    op.create_index("ix_safety_block_events_device_id", "safety_block_events", ["device_id"])
    op.create_index("ix_safety_block_events_user_id", "safety_block_events", ["user_id"])

    op.create_table(
        "attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("stored_filename", sa.String(length=100), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["diagnostic_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stored_filename"),
    )
    op.create_index("ix_attachments_session_id", "attachments", ["session_id"])

    op.create_table(
        "service_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("report_number", sa.String(length=50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["diagnostic_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_number"),
        sa.UniqueConstraint("session_id"),
    )

    op.create_table(
        "step_executions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["diagnostic_sessions.id"]),
        sa.ForeignKeyConstraint(["step_id"], ["diagnostic_steps.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "step_id"),
    )


def downgrade() -> None:
    op.drop_table("step_executions")
    op.drop_table("service_reports")
    op.drop_index("ix_attachments_session_id", table_name="attachments")
    op.drop_table("attachments")
    op.drop_index("ix_safety_block_events_user_id", table_name="safety_block_events")
    op.drop_index("ix_safety_block_events_device_id", table_name="safety_block_events")
    op.drop_index("ix_safety_block_events_category", table_name="safety_block_events")
    op.drop_table("safety_block_events")
    op.drop_index("ix_knowledge_chunks_document_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_table("diagnostic_steps")
    op.drop_index("ix_diagnostic_sessions_user_id", table_name="diagnostic_sessions")
    op.drop_index("ix_diagnostic_sessions_device_id", table_name="diagnostic_sessions")
    op.drop_table("diagnostic_sessions")
    op.drop_index("ix_user_devices_user_id", table_name="user_devices")
    op.drop_table("user_devices")
    op.drop_index("ix_knowledge_documents_sha256", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_robot_model_id", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
    op.drop_index("ix_diagnostic_flows_status", table_name="diagnostic_flows")
    op.drop_index("ix_diagnostic_flows_stable_key", table_name="diagnostic_flows")
    op.drop_table("diagnostic_flows")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.drop_index("ix_robot_models_code", table_name="robot_models")
    op.drop_table("robot_models")
    op.drop_table("pending_file_deletions")
    op.drop_index("ix_issue_categories_code", table_name="issue_categories")
    op.drop_table("issue_categories")
