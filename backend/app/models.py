from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
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
        CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),
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
            "scope IN ('email', 'email_ip', 'ip')", name="ck_login_throttles_scope"
        ),
        CheckConstraint(
            "(scope = 'email' AND email_hash IS NOT NULL AND client_ip_hash IS NULL) OR "
            "(scope = 'email_ip' AND email_hash IS NOT NULL AND client_ip_hash IS NOT NULL) OR "
            "(scope = 'ip' AND email_hash IS NULL AND client_ip_hash IS NOT NULL)",
            name="ck_login_throttles_scope_keys",
        ),
        Index("ix_login_throttles_email_scope", "email_hash", "scope"),
        Index("ix_login_throttles_ip_scope", "client_ip_hash", "scope"),
    )

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(20))
    email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
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


class ApiRateLimit(Base):
    __tablename__ = "api_rate_limits"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('user', 'email', 'ip')", name="ck_api_rate_limits_scope"
        ),
        CheckConstraint(
            "window_kind IN ('minute', 'day')",
            name="ck_api_rate_limits_window_kind",
        ),
        CheckConstraint(
            "request_count >= 0", name="ck_api_rate_limits_request_count_nonnegative"
        ),
        Index("ix_api_rate_limits_action_scope", "action", "scope"),
    )

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(80))
    scope: Mapped[str] = mapped_column(String(20))
    principal_hash: Mapped[str] = mapped_column(String(64))
    window_kind: Mapped[str] = mapped_column(String(20))
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
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
        CheckConstraint(
            "status IN ('draft', 'published', 'retired')",
            name="ck_diagnostic_flows_status",
        ),
        CheckConstraint("version > 0", name="ck_diagnostic_flows_version_positive"),
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
        CheckConstraint(
            "evidence_level IN ('direct', 'partial', 'none')",
            name="ck_diagnostic_steps_evidence_level",
        ),
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
    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'resolved', 'unresolved')",
            name="ck_diagnostic_sessions_status",
        ),
        CheckConstraint(
            "current_position IS NULL OR current_position >= 0",
            name="ck_diagnostic_sessions_current_position_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("user_devices.id"), index=True)
    flow_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_flows.id"))
    issue_description: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id"), nullable=True, index=True
    )
    category_decision: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="in_progress")
    current_position: Mapped[int | None] = mapped_column(Integer, default=1)
    resolved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    device: Mapped[UserDevice] = relationship()
    flow: Mapped[DiagnosticFlow] = relationship()
    source_conversation: Mapped["Conversation | None"] = relationship()
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
    __table_args__ = (
        CheckConstraint(
            "risk_level = 'critical'",
            name="ck_safety_block_events_risk_level",
        ),
    )

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
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('resolved', 'not_resolved')",
            name="ck_step_executions_outcome",
        ),
        UniqueConstraint("session_id", "step_id"),
    )

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


class GenerationRecord(Base):
    """生成层调用留痕：每次回答/拒答一行，可追溯可复算。"""

    __tablename__ = "generation_records"
    __table_args__ = (
        CheckConstraint(
            "status IN ('answered', 'refused')",
            name="ck_generation_records_status",
        ),
        CheckConstraint(
            "refusal_reason IN ('knowledge_gap', 'model_refused', 'citation_invalid', "
            "'unsafe_answer') OR refusal_reason IS NULL",
            name="ck_generation_records_refusal_reason",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    robot_model_id: Mapped[int] = mapped_column(ForeignKey("robot_models.id"), index=True)
    query: Mapped[str] = mapped_column(String(2000))
    prompt_version: Mapped[str] = mapped_column(String(40))
    provider_model: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), index=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    refusal_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    snippets_sha256: Mapped[str] = mapped_column(String(64))
    snippet_count: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeGapEvent(Base):
    """内容缺口事件：检索空结果 / 生成层资料缺口拒答各记一行，支撑内容缺口榜。

    只保存归一化后的查询与型号，不保存用户身份——缺口榜是运营数据，不追人。
    """

    __tablename__ = "knowledge_gap_events"
    __table_args__ = (
        CheckConstraint(
            "source IN ('search_empty', 'answer_knowledge_gap')",
            name="ck_knowledge_gap_events_source",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    robot_model_id: Mapped[int] = mapped_column(ForeignKey("robot_models.id"), index=True)
    query_normalized: Mapped[str] = mapped_column(String(2000))
    source: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class Conversation(Base):
    """智能客服多轮会话：一个用户对一个型号的连续问答上下文。"""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    robot_model_id: Mapped[int] = mapped_column(ForeignKey("robot_models.id"), index=True)
    title: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, index=True
    )

    messages: Mapped[list["ConversationMessage"]] = relationship(
        back_populates="conversation", order_by="ConversationMessage.id"
    )


class ConversationMessage(Base):
    """会话消息：assistant 消息带引用与拒答原因，可回溯到 GenerationRecord。"""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_conversation_messages_role",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    citations_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    refusal_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    generation_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("generation_records.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
