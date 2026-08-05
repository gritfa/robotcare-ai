from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    invite_code: str | None = Field(default=None, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserRead(ORMModel):
    id: int
    email: EmailStr
    role: str
    status: str
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


class AdminGenerationStatsRead(BaseModel):
    answered_count: int
    refused_count: int
    refusal_by_reason: dict[str, int]


class AdminOverviewRead(BaseModel):
    user_count: int
    active_model_count: int
    published_flow_count: int
    knowledge_document_count: int
    knowledge_chunk_count: int
    safety_block_count: int
    unresolved_diagnostic_count: int
    service_report_count: int
    generation_stats: AdminGenerationStatsRead
    content_gap_count: int


class AdminContentGapRead(BaseModel):
    """内容缺口榜条目：只含聚合后的查询与型号，不含任何用户信息。

    按 (型号, 问题) 聚合而不是只按问题：同一句话在不同型号下是两个缺口——
    要补的资料不同、复测也必须分别做，合并展示会让闭环无从下手。
    """

    query_normalized: str
    count: int
    robot_model_id: int
    model_code: str
    last_seen_at: datetime
    status: str = "open"
    linked_document_id: int | None = None
    linked_document_title: str | None = None
    replay_status: str | None = None
    replay_citation_count: int | None = None
    replay_answer_excerpt: str | None = None
    replay_checked_at: datetime | None = None
    resolved_at: datetime | None = None
    note: str | None = None


class AdminContentGapActionRequest(BaseModel):
    """缺口没有自己的主键：它由 (型号, 归一化问题) 唯一确定，定位信息随请求体走。"""

    robot_model_id: int
    query_normalized: str = Field(min_length=1, max_length=2000)


class AdminContentGapUpdate(AdminContentGapActionRequest):
    status: Literal["open", "investigating", "resolved", "wont_fix"] | None = None
    linked_document_id: int | None = None
    note: str | None = Field(default=None, max_length=500)


class AdminContentGapResolutionRead(BaseModel):
    status: str
    linked_document_id: int | None = None
    linked_document_title: str | None = None
    replay_status: str | None = None
    replay_citation_count: int | None = None
    replay_answer_excerpt: str | None = None
    replay_checked_at: datetime | None = None
    resolved_at: datetime | None = None
    note: str | None = None


class AdminContentGapReplayRequest(AdminContentGapActionRequest):
    auto_resolve: bool = True


class AdminContentGapReplayRead(BaseModel):
    replay_status: Literal["passed", "failed", "error"]
    citation_count: int
    answer_excerpt: str | None
    detail: str
    gap_status: str
    checked_at: datetime | None


class AdminKnowledgeUploadRead(BaseModel):
    document_id: int
    created: bool
    changed: bool
    chunk_count: int
    sha256: str


class AdminKnowledgeDocumentRead(BaseModel):
    id: int
    robot_model_id: int
    model_code: str
    title: str
    source_url: str
    sha256: str
    page_count: int
    chunk_count: int
    vector_count: int
    status: str
    version: int
    embedding_model: str | None
    file_size: int | None
    has_archived_file: bool
    created_at: datetime
    updated_at: datetime


class AdminKnowledgeDocumentVersionRead(BaseModel):
    version: int
    sha256: str
    title: str
    page_count: int
    chunk_count: int
    change_kind: str
    note: str | None
    has_archived_file: bool
    embedding_model: str | None
    created_at: datetime


class AdminKnowledgeDocumentDetailRead(AdminKnowledgeDocumentRead):
    versions: list[AdminKnowledgeDocumentVersionRead]


class AdminKnowledgeChunkRead(BaseModel):
    chunk_index: int
    page_number: int
    content: str
    has_embedding: bool


class AdminKnowledgeChunkPage(BaseModel):
    document_id: int
    total: int
    offset: int
    limit: int
    items: list[AdminKnowledgeChunkRead]


class AdminKnowledgeDocumentUpdate(BaseModel):
    status: Literal["active", "disabled"] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=255)


class AdminKnowledgeRollbackRequest(BaseModel):
    version: int = Field(ge=1)


class AdminKnowledgeDiffPreviewRead(BaseModel):
    status: Literal["new", "identical", "changed"]
    model_code: str
    source_url: str
    incoming_title: str
    incoming_sha256: str
    incoming_page_count: int
    incoming_chunk_count: int
    document_id: int | None = None
    current_version: int | None = None
    current_title: str | None = None
    current_sha256: str | None = None
    current_page_count: int | None = None
    current_chunk_count: int | None = None
    current_updated_at: datetime | None = None
    page_delta: int | None = None
    chunk_delta: int | None = None
    pages_comparable: bool = False
    pages_incomparable_reason: str | None = None
    changed_pages: list[int] = Field(default_factory=list)
    added_pages: list[int] = Field(default_factory=list)
    removed_pages: list[int] = Field(default_factory=list)


class AdminSafetyBlockRead(BaseModel):
    id: int
    model_code: str
    category: str
    risk_level: str
    created_at: datetime


class AdminSafetyBlockDetailRead(AdminSafetyBlockRead):
    user_id: int
    device_id: int
    reason: str
    advice: str


class AdminReportSummary(BaseModel):
    id: int
    report_number: str
    created_at: datetime


class AdminDiagnosticSummary(BaseModel):
    id: int
    status: str
    created_at: datetime


class AdminRobotModelSummary(BaseModel):
    id: int
    code: str
    name: str


class AdminUserSummary(BaseModel):
    email_masked: str


class AdminUnresolvedReportRead(BaseModel):
    report: AdminReportSummary
    diagnostic: AdminDiagnosticSummary
    model: AdminRobotModelSummary
    user: AdminUserSummary


class AdminServiceReportDetailRead(BaseModel):
    id: int
    session_id: int
    report_number: str
    content: str
    created_at: datetime


class AdminDiagnosticDetailRead(BaseModel):
    id: int
    user_id: int
    device_id: int
    flow_id: int
    status: str
    issue_description: str
    error_code: str | None
    created_at: datetime


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
    confirm_category_mismatch: bool = False
    source_conversation_id: int | None = Field(default=None, gt=0)


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
    source_conversation_id: int | None = None
    category_decision: dict[str, object] = Field(default_factory=dict)
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
    model_config = ConfigDict(extra="forbid")

    robot_model_id: int = Field(gt=0)
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)


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


class KnowledgeModelHealthRead(KnowledgeStatusRead):
    document_sha256s: list[str]
    ready: bool


class KnowledgeProbeRead(BaseModel):
    """真实外部调用探测结果：每项都由一次实际请求证明，不是配置检查。"""

    embedding_service: bool
    retrieval_end_to_end: bool
    generation_service: bool
    errors: list[str]


class KnowledgeHealthRead(BaseModel):
    status: Literal["normal", "knowledge_degraded", "external_model_unavailable"]
    ready: bool
    embedding_configured: bool
    models: list[KnowledgeModelHealthRead]
    probe: KnowledgeProbeRead | None = None


class KnowledgeAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    robot_model_id: int = Field(gt=0)
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=5)


class AnswerCitationRead(BaseModel):
    index: int
    source_url: str
    page_number: int
    score: float
    document_sha256: str
    # 证据抽屉要展示的原文与文档名；路由层上线前的历史消息没有这两项，给空串兜底
    snippet: str = ""
    document_title: str = ""


class KnowledgeAnswerResponse(BaseModel):
    status: str
    answer: str | None
    citations: list[AnswerCitationRead]
    refusal_reason: str | None
    record_id: int


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    robot_model_id: int = Field(gt=0)


class ConversationRead(BaseModel):
    id: int
    robot_model_id: int
    robot_model_code: str
    title: str
    resolved: bool | None = None
    updated_at: datetime


class ConversationUpdateRequest(BaseModel):
    """改标题与标记已解决共用一个 PATCH；两者都不传即无操作。"""

    title: str | None = Field(default=None, min_length=1, max_length=120)
    resolved: bool | None = None


class MessageFeedbackRequest(BaseModel):
    helpful: bool
    # 原因固定枚举而非自由文本，才能在管理端按原因聚合出优化优先级
    reason: (
        Literal[
            "off_topic", "unclear_steps", "wrong_citation", "wrong_model", "still_unresolved"
        ]
        | None
    ) = None


class MessageFeedbackRead(BaseModel):
    message_id: int
    helpful: bool
    reason: str | None


class ConversationMessageRead(BaseModel):
    id: int
    role: str
    content: str
    citations: list[AnswerCitationRead]
    refusal_reason: str | None
    # 路由层结论：intent 决定前端怎么渲染这条消息，action_code 非空时渲染操作按钮。
    # 历史消息（路由层上线前）两者为 None，前端按普通回答渲染。
    intent: str | None = None
    action_code: str | None = None
    # 回答下方的快捷操作（系统建议的下一步），[{code,label,...}]
    quick_actions: list[dict] = Field(default_factory=list)
    created_at: datetime


class ConversationDetailRead(BaseModel):
    id: int
    robot_model_id: int
    robot_model_code: str
    title: str
    messages: list[ConversationMessageRead]


class ChatMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=2000)


class ChatMessageResponse(BaseModel):
    user_message: ConversationMessageRead
    assistant_message: ConversationMessageRead
