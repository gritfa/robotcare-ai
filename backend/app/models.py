from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .db_types import EmbeddingVector


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_users_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="user")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    revoke_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('email', 'email_ip')", name="ck_login_throttles_scope"
        ),
        Index("ix_login_throttles_email_scope", "email_hash", "scope"),
    )

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(20))
    email_hash: Mapped[str] = mapped_column(String(64))
    client_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_resource", "resource_type", "resource_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(100), index=True)
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    details_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class RobotModel(Base):
    __tablename__ = "robot_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    brand: Mapped[str] = mapped_column(String(50), default="海尔")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (UniqueConstraint("robot_model_id", "source_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    robot_model_id: Mapped[int] = mapped_column(ForeignKey("robot_models.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str] = mapped_column(String(2000))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    page_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    robot_model: Mapped[RobotModel] = relationship()
    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        back_populates="document",
        order_by="KnowledgeChunk.chunk_index",
        cascade="all, delete-orphan",
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    page_number: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | str | None] = mapped_column(EmbeddingVector(), nullable=True)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")


class UserDevice(Base):
    __tablename__ = "user_devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    robot_model_id: Mapped[int] = mapped_column(ForeignKey("robot_models.id"))
    nickname: Mapped[str] = mapped_column(String(100))
    serial_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    robot_model: Mapped[RobotModel] = relationship()


class IssueCategory(Base):
    __tablename__ = "issue_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))


class DiagnosticFlow(Base):
    __tablename__ = "diagnostic_flows"
    __table_args__ = (
        UniqueConstraint("stable_key", "version"),
        UniqueConstraint("robot_model_id", "issue_category_id", "version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stable_key: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[int] = mapped_column(Integer)
    robot_model_id: Mapped[int] = mapped_column(ForeignKey("robot_models.id"))
    issue_category_id: Mapped[int] = mapped_column(ForeignKey("issue_categories.id"))
    title: Mapped[str] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    content_sha256: Mapped[str] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    issue_category: Mapped[IssueCategory] = relationship()
    steps: Mapped[list[DiagnosticStep]] = relationship(
        back_populates="flow", order_by="DiagnosticStep.position", cascade="all, delete-orphan"
    )


class DiagnosticStep(Base):
    __tablename__ = "diagnostic_steps"
    __table_args__ = (
        UniqueConstraint("flow_id", "position"),
        UniqueConstraint("flow_id", "stable_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    flow_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_flows.id"))
    stable_key: Mapped[str] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(150))
    instruction: Mapped[str] = mapped_column(Text)
    source_label: Mapped[str] = mapped_column(String(200), default="官方说明书")
    source_url: Mapped[str] = mapped_column(String(2000))
    source_page: Mapped[int] = mapped_column(Integer)
    evidence_level: Mapped[str] = mapped_column(String(20))
    evidence_basis: Mapped[str] = mapped_column(Text)
    policy_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    flow: Mapped[DiagnosticFlow] = relationship(back_populates="steps")


class DiagnosticSession(Base):
    __tablename__ = "diagnostic_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("user_devices.id"), index=True)
    flow_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_flows.id"))
    issue_description: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    category_decision: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="in_progress")
    current_position: Mapped[int | None] = mapped_column(Integer, default=1)
    resolved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    device: Mapped[UserDevice] = relationship()
    flow: Mapped[DiagnosticFlow] = relationship()
    executions: Mapped[list[StepExecution]] = relationship(
        back_populates="session", order_by="StepExecution.created_at", cascade="all, delete-orphan"
    )
    report: Mapped[ServiceReport | None] = relationship(back_populates="session", uselist=False)
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="session", order_by="Attachment.created_at", cascade="all, delete-orphan"
    )

    @property
    def report_available(self) -> bool:
        return self.report is not None

    @property
    def flow_stable_key(self) -> str:
        return self.flow.stable_key

    @property
    def flow_version(self) -> int:
        return self.flow.version


class SafetyBlockEvent(Base):
    __tablename__ = "safety_block_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("user_devices.id"), index=True)
    category: Mapped[str] = mapped_column(String(80), index=True)
    risk_level: Mapped[str] = mapped_column(String(20), default="critical")
    reason: Mapped[str] = mapped_column(String(500))
    advice: Mapped[str] = mapped_column(String(500))
    description_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StepExecution(Base):
    __tablename__ = "step_executions"
    __table_args__ = (UniqueConstraint("session_id", "step_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_sessions.id"))
    step_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_steps.id"))
    outcome: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[DiagnosticSession] = relationship(back_populates="executions")
    step: Mapped[DiagnosticStep] = relationship()


class ServiceReport(Base):
    __tablename__ = "service_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_sessions.id"), unique=True)
    report_number: Mapped[str] = mapped_column(String(50), unique=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[DiagnosticSession] = relationship(back_populates="report")


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_sessions.id"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(100), unique=True)
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[DiagnosticSession] = relationship(back_populates="attachments")


class PendingFileDeletion(Base):
    __tablename__ = "pending_file_deletions"

    id: Mapped[int] = mapped_column(primary_key=True)
    original_stored_filename: Mapped[str] = mapped_column(String(255))
    quarantined_filename: Mapped[str] = mapped_column(String(255), unique=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
