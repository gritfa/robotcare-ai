from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserRead(ORMModel):
    id: int
    email: EmailStr
    role: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserRead


class ModelRead(ORMModel):
    id: int
    code: str
    name: str
    brand: str


class AdminModelRead(ModelRead):
    active: bool


class AdminModelUpdate(BaseModel):
    active: bool


class AdminOverviewRead(BaseModel):
    user_count: int
    active_model_count: int
    published_flow_count: int
    knowledge_document_count: int
    knowledge_chunk_count: int
    safety_block_count: int
    unresolved_diagnostic_count: int
    service_report_count: int


class AdminSafetyBlockRead(BaseModel):
    id: int
    user_id: int
    device_id: int
    model_code: str
    category: str
    risk_level: str
    reason: str
    advice: str
    created_at: datetime


class AdminReportSummary(BaseModel):
    id: int
    report_number: str
    created_at: datetime


class AdminDiagnosticSummary(BaseModel):
    id: int
    status: str
    issue_description: str
    error_code: str | None
    created_at: datetime


class AdminRobotModelSummary(BaseModel):
    id: int
    code: str
    name: str


class AdminUserSummary(BaseModel):
    id: int
    email_masked: str


class AdminUnresolvedReportRead(BaseModel):
    report: AdminReportSummary
    diagnostic: AdminDiagnosticSummary
    model: AdminRobotModelSummary
    user: AdminUserSummary


class AdminAuditLogRead(ORMModel):
    id: int
    actor_user_id: int | None
    action: str
    resource_type: str
    resource_id: str | None
    details_json: dict[str, object]
    created_at: datetime


class DiagnosticOptionRead(BaseModel):
    stable_key: str
    version: int
    issue_category_code: str
    issue_category_name: str
    title: str


class DeviceCreate(BaseModel):
    robot_model_id: int
    nickname: str = Field(min_length=1, max_length=100)
    serial_number: str | None = Field(default=None, max_length=100)


class DeviceUpdate(BaseModel):
    nickname: str | None = Field(default=None, min_length=1, max_length=100)
    serial_number: str | None = Field(default=None, max_length=100)


class DeviceRead(ORMModel):
    id: int
    robot_model_id: int
    nickname: str
    serial_number: str | None
    robot_model: ModelRead
    created_at: datetime


class DiagnosticCreate(BaseModel):
    device_id: int
    issue_category_code: str
    issue_description: str = Field(min_length=3, max_length=4000)
    error_code: str | None = Field(default=None, max_length=100)


class StepRead(ORMModel):
    id: int
    stable_key: str
    position: int
    title: str
    instruction: str
    source_label: str
    source_url: str
    source_page: int
    evidence_level: str
    evidence_basis: str
    policy_note: str | None


class ExecutionRead(ORMModel):
    id: int
    outcome: str
    step: StepRead
    created_at: datetime


class DiagnosticRead(ORMModel):
    id: int
    device_id: int
    issue_description: str
    error_code: str | None
    status: str
    current_position: int | None
    resolved: bool | None
    report_available: bool = False
    flow_stable_key: str
    flow_version: int
    created_at: datetime
    updated_at: datetime
    executions: list[ExecutionRead] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    step_id: int = Field(gt=0)
    outcome: Literal["resolved", "not_resolved"]


class FeedbackResponse(BaseModel):
    diagnostic: DiagnosticRead
    current_step: StepRead | None


class ReportRead(ORMModel):
    id: int
    session_id: int
    report_number: str
    content: str
    created_at: datetime


class ReportPdfRead(BaseModel):
    report_id: int
    report_number: str
    filename: str
    size_bytes: int
    download_url: str


class AttachmentRead(ORMModel):
    id: int
    session_id: int
    original_filename: str
    content_type: str
    size_bytes: int
    created_at: datetime


class KnowledgeSearchRequest(BaseModel):
    robot_model_id: int = Field(gt=0)
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    min_score: float = Field(default=0.25, ge=0, le=1)


class KnowledgeSearchResult(BaseModel):
    score: float
    content: str
    document_title: str
    source_url: str
    page_number: int


class KnowledgeStatusRead(BaseModel):
    robot_model_id: int
    model_code: str
    document_count: int
    chunk_count: int
    vector_count: int
