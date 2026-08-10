"""/diagnostics* routes: sessions, steps, feedback, attachments, reports, PDFs."""

import hashlib
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..alerting import send_alert
from ..attachment_service import create_attachment as persist_attachment
from ..database import get_db
from ..diagnostic_graph import feedback_decision_graph
from ..issue_classifier import classify_issue
from ..models import (
    Attachment,
    Conversation,
    DiagnosticFlow,
    DiagnosticSession,
    DiagnosticStep,
    IssueCategory,
    PendingFileDeletion,
    SafetyBlockEvent,
    ServiceReport,
    StepExecution,
    User,
)
from ..observability import emit_json_log, request_trace_id
from ..pdf_report import ensure_report_pdf as ensure_stored_report_pdf, report_pdf_path
from ..rate_limit_service import enforce_business_rate_limit
from ..report_service import get_or_create_service_report as persist_service_report
from ..safety import detect_safety_block
from ..schemas import (
    AttachmentRead,
    DiagnosticCreate,
    DiagnosticRead,
    FeedbackRequest,
    FeedbackResponse,
    ReportPdfRead,
    ReportRead,
    StepRead,
)
from ..security import get_current_user
from ._shared import (
    ALLOWED_IMAGE_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_DIAGNOSTIC,
    current_step_for,
    owned_device,
    owned_diagnostic,
    safety_block_http_exception,
    validate_image_content,
)

router = APIRouter(prefix="/api/v1")

DIAGNOSTIC_PAGE_SIZE = 100
MAX_DIAGNOSTIC_PAGE_SIZE = 200


@router.post("/diagnostics", response_model=DiagnosticRead, status_code=201)
def create_diagnostic(
    payload: DiagnosticCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DiagnosticSession:
    device = owned_device(db, payload.device_id, user)
    if not device.robot_model.active:
        raise HTTPException(status_code=409, detail="Robot model is inactive")
    if payload.source_conversation_id is not None:
        source_conversation = db.get(Conversation, payload.source_conversation_id)
        # 404 而非 403：不向他人泄露会话是否存在（与会话路由同语义）
        if source_conversation is None or source_conversation.user_id != user.id:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if source_conversation.robot_model_id != device.robot_model_id:
            raise HTTPException(
                status_code=409,
                detail="Conversation robot model does not match the selected device",
            )
    safety_block = detect_safety_block(
        " ".join(part for part in (payload.issue_description, payload.error_code) if part)
    )
    if safety_block is not None:
        db.add(
            SafetyBlockEvent(
                user_id=user.id,
                device_id=device.id,
                category=safety_block.category,
                risk_level=safety_block.risk_level,
                reason=safety_block.reason,
                advice=safety_block.advice,
                description_sha256=hashlib.sha256(
                    payload.issue_description.encode("utf-8")
                ).hexdigest(),
            )
        )
        db.commit()
        send_alert(
            f"safety_block:{safety_block.category}",
            f"触发高危阻断：{safety_block.category}（详情见管理员后台安全阻断页）",
        )
        emit_json_log(
            logging.WARNING,
            "safety_block",
            trace_id=request_trace_id(request),
            user_id=user.id,
            device_id=device.id,
            category=safety_block.category,
            risk_level=safety_block.risk_level,
        )
        raise safety_block_http_exception(safety_block)
    enforce_business_rate_limit(
        db,
        request,
        action="diagnostic_create",
        user_id=user.id,
    )
    flow = db.scalar(
        select(DiagnosticFlow)
        .join(IssueCategory)
        .options(selectinload(DiagnosticFlow.steps))
        .where(
            DiagnosticFlow.robot_model_id == device.robot_model_id,
            IssueCategory.code == payload.issue_category_code,
            DiagnosticFlow.status == "published",
            DiagnosticFlow.active.is_(True),
        )
    )
    if flow is None or not flow.steps:
        raise HTTPException(status_code=404, detail="No diagnostic flow for this model and issue")
    available_category_codes = set(
        db.scalars(
            select(IssueCategory.code)
            .join(DiagnosticFlow)
            .where(
                DiagnosticFlow.robot_model_id == device.robot_model_id,
                DiagnosticFlow.status == "published",
                DiagnosticFlow.active.is_(True),
            )
        )
    )
    category_decision = classify_issue(
        model_code=device.robot_model.code,
        selected_category_code=payload.issue_category_code,
        issue_description=payload.issue_description,
        error_code=payload.error_code,
        available_category_codes=available_category_codes,
    )
    confirmed_ambiguous = (
        category_decision.kind == "ambiguous"
        and payload.confirm_category_mismatch
    )
    if category_decision.kind == "conflict" or (
        category_decision.kind == "ambiguous" and not confirmed_ambiguous
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ISSUE_CATEGORY_MISMATCH",
                "selected": payload.issue_category_code,
                "suggested": category_decision.suggested_category_code,
                "suggested_categories": [
                    candidate.category_code for candidate in category_decision.candidates
                ],
                "requires_confirmation": category_decision.kind == "ambiguous",
            },
        )
    diagnostic = DiagnosticSession(
        user_id=user.id,
        device_id=device.id,
        flow_id=flow.id,
        issue_description=payload.issue_description,
        error_code=payload.error_code,
        source_conversation_id=payload.source_conversation_id,
        category_decision=category_decision.metadata(
            confirmation="keep_selected" if confirmed_ambiguous else None
        ),
        status="in_progress",
        current_position=flow.steps[0].position,
    )
    db.add(diagnostic)
    db.commit()
    emit_json_log(
        logging.INFO,
        "diagnostic_created",
        trace_id=request_trace_id(request),
        diagnostic_id=diagnostic.id,
        user_id=user.id,
        device_id=device.id,
        flow_id=flow.id,
        flow_version=flow.version,
        status=diagnostic.status,
    )
    return owned_diagnostic(db, diagnostic.id, user)


@router.get("/diagnostics", response_model=list[DiagnosticRead])
def list_diagnostics(
    limit: int = Query(default=DIAGNOSTIC_PAGE_SIZE, ge=1, le=MAX_DIAGNOSTIC_PAGE_SIZE),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[DiagnosticSession]:
    # 每条诊断还要 selectinload 出全部步骤执行记录与报告，无 limit 的话
    # 一个老账号打开「诊断历史」就是一次全表级读取（体检 D5）
    stmt = (
        select(DiagnosticSession)
        .options(
            selectinload(DiagnosticSession.executions).selectinload(StepExecution.step),
            selectinload(DiagnosticSession.report),
        )
        .where(DiagnosticSession.user_id == user.id)
        .order_by(DiagnosticSession.created_at.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


@router.get("/diagnostics/{diagnostic_id}", response_model=DiagnosticRead)
def get_diagnostic(
    diagnostic_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> DiagnosticSession:
    return owned_diagnostic(db, diagnostic_id, user)


@router.get("/diagnostics/{diagnostic_id}/steps/current", response_model=StepRead)
def get_current_step(
    diagnostic_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> DiagnosticStep:
    diagnostic = owned_diagnostic(db, diagnostic_id, user)
    step = current_step_for(diagnostic)
    if step is None:
        raise HTTPException(status_code=409, detail="Diagnostic is already finished")
    return step


@router.post("/diagnostics/{diagnostic_id}/feedback", response_model=FeedbackResponse)
def submit_feedback(
    diagnostic_id: int,
    payload: FeedbackRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FeedbackResponse:
    diagnostic = owned_diagnostic(db, diagnostic_id, user)
    step = current_step_for(diagnostic)
    if step is None:
        raise HTTPException(status_code=409, detail="Diagnostic is already finished or feedback was submitted")
    if payload.step_id != step.id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STALE_DIAGNOSTIC_STEP",
                "message": "Feedback does not match the current diagnostic step",
                "current_step_id": step.id,
            },
        )

    decision = feedback_decision_graph.invoke(
        {
            "current_position": step.position,
            "step_positions": [item.position for item in diagnostic.flow.steps],
            "outcome": payload.outcome,
        }
    )
    updated = db.execute(
        update(DiagnosticSession)
        .where(
            DiagnosticSession.id == diagnostic.id,
            DiagnosticSession.status == "in_progress",
            DiagnosticSession.current_position == step.position,
        )
        .values(
            status=decision["status"],
            resolved=decision["resolved"],
            current_position=decision["next_position"],
        )
        .execution_options(synchronize_session=False)
    )
    if updated.rowcount != 1:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "FEEDBACK_CONFLICT",
                "message": "This diagnostic step was already processed",
            },
        )
    db.add(StepExecution(session_id=diagnostic.id, step_id=step.id, outcome=payload.outcome))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "FEEDBACK_CONFLICT",
                "message": "This diagnostic step was already processed",
            },
        ) from exc
    db.expire_all()
    refreshed = owned_diagnostic(db, diagnostic.id, user)
    emit_json_log(
        logging.INFO,
        "diagnostic_state_changed",
        trace_id=request_trace_id(request),
        diagnostic_id=diagnostic.id,
        user_id=user.id,
        step_id=step.id,
        outcome=payload.outcome,
        previous_status="in_progress",
        status=refreshed.status,
        next_position=refreshed.current_position,
    )
    return FeedbackResponse(diagnostic=refreshed, current_step=current_step_for(refreshed))


def make_report(diagnostic: DiagnosticSession) -> str:
    category_decision = diagnostic.category_decision or {}
    candidates = category_decision.get("candidate_category_codes") or []
    lines = [
        "RobotCare AI 第三方售后诊断报告",
        f"设备型号：{diagnostic.device.robot_model.code}",
        f"用户问题：{diagnostic.issue_description}",
        f"错误码：{diagnostic.error_code or '未提供'}",
        f"用户选择类别：{category_decision.get('selected_category_code', diagnostic.flow.issue_category.code)}",
        f"规则候选类别：{', '.join(str(item) for item in candidates) or '无明确候选'}",
        f"最终类别：{category_decision.get('final_category_code', diagnostic.flow.issue_category.code)}",
        "已执行的安全排查步骤：",
    ]
    for index, execution in enumerate(diagnostic.executions, start=1):
        result = "已解决" if execution.outcome == "resolved" else "未解决"
        lines.append(f"{index}. {execution.step.title} - {result}")
        lines.append(f"   操作：{execution.step.instruction}")
        lines.append(f"   来源：{execution.step.source_label}")
    lines.append("附件文件：")
    if diagnostic.attachments:
        lines.extend(f"- {attachment.original_filename}" for attachment in diagnostic.attachments)
    else:
        lines.append("- 未提供")
    lines.extend(conversation_summary_lines(diagnostic.source_conversation))
    lines.extend(
        [
            "最终结果：自助排查未解决，建议联系海尔官方售后。",
            "免责声明：本报告由独立第三方工具生成，不代表海尔官方诊断结论。",
        ]
    )
    return "\n".join(lines)


REPORT_CONVERSATION_MESSAGE_LIMIT = 12
REPORT_CONVERSATION_SNIPPET_CHARS = 80


def conversation_summary_lines(conversation: Conversation | None) -> list[str]:
    """未解决会话的问答摘要，附入售后报告给人工售后当上下文。"""
    if conversation is None or not conversation.messages:
        return []
    messages = conversation.messages[-REPORT_CONVERSATION_MESSAGE_LIMIT:]
    omitted = len(conversation.messages) - len(messages)
    lines = [f"此前智能客服会话摘要（会话 #{conversation.id}）："]
    if omitted:
        lines.append(f"（更早的 {omitted} 条消息略）")
    for index, message in enumerate(messages, start=1):
        snippet = message.content.replace("\n", " ").strip()
        if len(snippet) > REPORT_CONVERSATION_SNIPPET_CHARS:
            snippet = snippet[:REPORT_CONVERSATION_SNIPPET_CHARS] + "…"
        if message.role == "user":
            lines.append(f"{index}. [用户] {snippet}")
        elif message.refusal_reason:
            lines.append(f"{index}. [AI·拒答] {snippet}")
        else:
            pages = "、".join(
                str(citation.get("page_number"))
                for citation in (message.citations_json or [])
                if citation.get("page_number")
            )
            source = f"（引用说明书第 {pages} 页）" if pages else ""
            lines.append(f"{index}. [AI] {snippet}{source}")
    return lines


def get_or_create_service_report(db: Session, diagnostic: DiagnosticSession) -> ServiceReport:
    return persist_service_report(db, diagnostic, make_report)


def ensure_report_pdf(request: Request, report: ServiceReport) -> Path:
    return ensure_stored_report_pdf(
        request.app.state.report_dir,
        report,
        request.app.state.report_filename_secret,
    )


def report_pdf_response(diagnostic_id: int, report: ServiceReport, target: Path) -> ReportPdfRead:
    return ReportPdfRead(
        report_id=report.id,
        report_number=report.report_number,
        filename=f"RobotCare-{report.report_number}.pdf",
        size_bytes=target.stat().st_size,
        download_url=f"/api/v1/diagnostics/{diagnostic_id}/report/pdf",
    )


@router.post(
    "/diagnostics/{diagnostic_id}/attachments", response_model=AttachmentRead, status_code=201
)
async def upload_attachment(
    diagnostic_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Attachment:
    diagnostic = owned_diagnostic(db, diagnostic_id, user)
    if diagnostic.status != "in_progress":
        raise HTTPException(status_code=409, detail="Attachments cannot be added after diagnosis ends")
    enforce_business_rate_limit(
        db,
        request,
        action="attachment_upload",
        user_id=user.id,
    )
    attachment_count = db.scalar(
        select(func.count()).select_from(Attachment).where(Attachment.session_id == diagnostic.id)
    )
    if attachment_count >= MAX_ATTACHMENTS_PER_DIAGNOSTIC:
        raise HTTPException(status_code=409, detail="A diagnostic can contain at most five images")
    original_filename = Path(file.filename or "").name
    if not original_filename or len(original_filename) > 255:
        raise HTTPException(status_code=400, detail="Invalid attachment filename")

    extension = Path(original_filename).suffix.lower()
    expected_content_type = ALLOWED_IMAGE_TYPES.get(extension)
    if expected_content_type is None or file.content_type != expected_content_type:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only jpg, jpeg, png, and webp images are allowed",
        )

    content = await file.read(MAX_ATTACHMENT_BYTES + 1)
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Image exceeds 5MB")
    if not content:
        raise HTTPException(status_code=400, detail="Empty attachment is not allowed")
    validate_image_content(content, expected_content_type)

    return persist_attachment(
        db,
        diagnostic,
        request.app.state.attachment_dir,
        original_filename=original_filename,
        extension=extension,
        content_type=expected_content_type,
        content=content,
        maximum_per_diagnostic=MAX_ATTACHMENTS_PER_DIAGNOSTIC,
    )


@router.get("/diagnostics/{diagnostic_id}/attachments", response_model=list[AttachmentRead])
def list_attachments(
    diagnostic_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Attachment]:
    diagnostic = owned_diagnostic(db, diagnostic_id, user)
    return list(
        db.scalars(
            select(Attachment)
            .where(Attachment.session_id == diagnostic.id)
            .order_by(Attachment.created_at, Attachment.id)
        )
    )


@router.delete("/diagnostics/{diagnostic_id}/attachments/{attachment_id}", status_code=204)
def delete_attachment(
    diagnostic_id: int,
    attachment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    diagnostic = owned_diagnostic(db, diagnostic_id, user)
    attachment = db.scalar(
        select(Attachment).where(
            Attachment.id == attachment_id,
            Attachment.session_id == diagnostic.id,
        )
    )
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    stored_filename = attachment.stored_filename
    target = request.app.state.attachment_dir / stored_filename
    if not target.is_file():
        raise HTTPException(status_code=409, detail="Attachment file is missing; deletion was not applied")
    quarantined_filename = f".pending-delete-{uuid4().hex}-{stored_filename}"
    quarantined = request.app.state.attachment_dir / quarantined_filename
    try:
        target.replace(quarantined)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Attachment file could not be quarantined") from exc

    pending = PendingFileDeletion(
        original_stored_filename=stored_filename,
        quarantined_filename=quarantined_filename,
    )
    try:
        db.add(pending)
        db.delete(attachment)
        db.commit()
    except Exception:
        db.rollback()
        if quarantined.exists():
            quarantined.replace(target)
        raise

    try:
        quarantined.unlink()
    except OSError as exc:
        pending.last_error = str(exc)[:500]
        db.add(pending)
        db.commit()
    else:
        db.delete(pending)
        db.commit()
    return Response(status_code=204)


@router.post("/diagnostics/{diagnostic_id}/report", response_model=ReportRead, status_code=201)
def create_report(
    diagnostic_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ServiceReport:
    diagnostic = owned_diagnostic(db, diagnostic_id, user)
    enforce_business_rate_limit(
        db,
        request,
        action="report_create",
        user_id=user.id,
    )
    return get_or_create_service_report(db, diagnostic)


@router.get("/diagnostics/{diagnostic_id}/report", response_model=ReportRead)
def get_report(
    diagnostic_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ServiceReport:
    diagnostic = owned_diagnostic(db, diagnostic_id, user)
    report = db.scalar(select(ServiceReport).where(ServiceReport.session_id == diagnostic.id))
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.post("/diagnostics/{diagnostic_id}/report/pdf", response_model=ReportPdfRead, status_code=201)
def create_report_pdf(
    diagnostic_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReportPdfRead:
    diagnostic = owned_diagnostic(db, diagnostic_id, user)
    enforce_business_rate_limit(
        db,
        request,
        action="pdf_create",
        user_id=user.id,
    )
    report = get_or_create_service_report(db, diagnostic)
    target = ensure_report_pdf(request, report)
    return report_pdf_response(diagnostic_id, report, target)


@router.get("/diagnostics/{diagnostic_id}/report/pdf")
def download_report_pdf(
    diagnostic_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    diagnostic = owned_diagnostic(db, diagnostic_id, user)
    report = db.scalar(select(ServiceReport).where(ServiceReport.session_id == diagnostic.id))
    if report is None:
        raise HTTPException(status_code=404, detail="PDF report not found")
    target = report_pdf_path(
        request.app.state.report_dir,
        report,
        request.app.state.report_filename_secret,
    )
    if not target.is_file():
        raise HTTPException(status_code=404, detail="PDF report not found")
    return FileResponse(
        target,
        media_type="application/pdf",
        filename=f"RobotCare-{report.report_number}.pdf",
    )
