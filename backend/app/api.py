import hashlib
import hmac
from io import BytesIO
import logging
from pathlib import Path
from time import perf_counter
from uuid import uuid4
import warnings

import jwt
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .auth_service import (
    REFRESH_COOKIE_NAME,
    acquire_login_attempt_lock,
    clear_login_failures,
    delete_refresh_cookie,
    enforce_login_rate_limit,
    issue_authentication,
    logout_refresh_token,
    normalize_email,
    record_login_failure,
    revoke_session,
    rotate_refresh_token,
    set_refresh_cookie,
)
from .config import Settings, get_settings
from .database import get_db
from .diagnostic_graph import feedback_decision_graph
from .issue_classifier import classify_issue
from .alerting import send_alert
from .generation_service import generate_answer
from .knowledge_service import get_knowledge_health, get_knowledge_status, search_knowledge
from .models import (
    Attachment,
    AuditLog,
    DiagnosticFlow,
    DiagnosticSession,
    DiagnosticStep,
    IssueCategory,
    KnowledgeChunk,
    KnowledgeDocument,
    PendingFileDeletion,
    RobotModel,
    SafetyBlockEvent,
    ServiceReport,
    StepExecution,
    User,
    UserDevice,
)
from .observability import emit_json_log, request_trace_id
from .attachment_service import create_attachment as persist_attachment
from .pdf_report import ensure_report_pdf as ensure_stored_report_pdf, report_pdf_path
from .report_service import get_or_create_service_report as persist_service_report
from .rate_limit_service import (
    aggregate_knowledge_version,
    enforce_business_rate_limit,
    enforce_embedding_rate_limit,
    enforce_registration_rate_limit,
    knowledge_cache_key,
    normalize_knowledge_query,
)
from .safety import detect_safety_block
from .schemas import (
    AnswerCitationRead,
    AttachmentRead,
    AdminAuditLogRead,
    AdminDiagnosticDetailRead,
    AdminDiagnosticSummary,
    AdminModelRead,
    AdminModelUpdate,
    AdminOverviewRead,
    AdminReportSummary,
    AdminRobotModelSummary,
    AdminSafetyBlockRead,
    AdminSafetyBlockDetailRead,
    AdminServiceReportDetailRead,
    AdminUnresolvedReportRead,
    AdminUserSummary,
    DeviceCreate,
    DeviceRead,
    DeviceUpdate,
    DiagnosticCreate,
    DiagnosticRead,
    DiagnosticOptionRead,
    FeedbackRequest,
    FeedbackResponse,
    LoginRequest,
    KnowledgeAnswerRequest,
    KnowledgeAnswerResponse,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
    KnowledgeHealthRead,
    KnowledgeModelHealthRead,
    KnowledgeStatusRead,
    ModelRead,
    RegisterRequest,
    ReportPdfRead,
    ReportRead,
    StepRead,
    TokenResponse,
    UserRead,
)
from .security import (
    DUMMY_PASSWORD_HASH,
    decode_access_token,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)

router = APIRouter(prefix="/api/v1")

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENTS_PER_DIAGNOSTIC = 5
MAX_IMAGE_PIXELS = 25_000_000
ALLOWED_IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
PIL_FORMAT_TO_CONTENT_TYPE = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


def validate_image_content(content: bytes, expected_content_type: str) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                detected_content_type = PIL_FORMAT_TO_CONTENT_TYPE.get(image.format or "")
                if detected_content_type != expected_content_type:
                    raise HTTPException(
                        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                        detail="Image extension, MIME type, and decoded format must match",
                    )
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail="Image pixel count exceeds the safe limit",
                    )
                image.verify()
            with Image.open(BytesIO(content)) as decoded:
                decoded.load()
    except HTTPException:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Image pixel count exceeds the safe limit",
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Uploaded file is not a valid decodable image",
        ) from exc


def token_response(user: User, access_token: str) -> TokenResponse:
    return TokenResponse(access_token=access_token, user=user)


def owned_device(db: Session, device_id: int, user: User) -> UserDevice:
    device = db.scalar(
        select(UserDevice).options(selectinload(UserDevice.robot_model)).where(UserDevice.id == device_id)
    )
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.user_id != user.id:
        raise HTTPException(status_code=403, detail="You do not own this device")
    return device


def owned_diagnostic(db: Session, diagnostic_id: int, user: User) -> DiagnosticSession:
    diagnostic = db.scalar(
        select(DiagnosticSession)
        .options(
            selectinload(DiagnosticSession.executions).selectinload(StepExecution.step),
            selectinload(DiagnosticSession.flow).selectinload(DiagnosticFlow.steps),
            selectinload(DiagnosticSession.device).selectinload(UserDevice.robot_model),
            selectinload(DiagnosticSession.attachments),
            selectinload(DiagnosticSession.report),
        )
        .where(DiagnosticSession.id == diagnostic_id)
    )
    if diagnostic is None:
        raise HTTPException(status_code=404, detail="Diagnostic not found")
    if diagnostic.user_id != user.id:
        raise HTTPException(status_code=403, detail="You do not own this diagnostic")
    return diagnostic


def current_step_for(diagnostic: DiagnosticSession) -> DiagnosticStep | None:
    if diagnostic.status != "in_progress" or diagnostic.current_position is None:
        return None
    return next((step for step in diagnostic.flow.steps if step.position == diagnostic.current_position), None)


def enforce_registration_policy(payload: RegisterRequest, settings: Settings) -> None:
    if settings.registration_mode == "open":
        return
    if settings.registration_mode == "closed":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Registration is not available")

    supplied_code = payload.invite_code or ""
    expected_code = settings.registration_invite_secret or ""
    if not hmac.compare_digest(supplied_code.encode("utf-8"), expected_code.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Registration is not available")


@router.post("/auth/register", response_model=TokenResponse, status_code=201)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    email = normalize_email(str(payload.email))
    enforce_registration_rate_limit(db, request, email)
    enforce_registration_policy(payload, get_settings())
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(email=email, password_hash=hash_password(payload.password))
    db.add(user)
    try:
        db.flush()
        issued = issue_authentication(db, user)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered") from None
    db.refresh(user)
    set_refresh_cookie(response, issued.refresh_token)
    return token_response(user, issued.access_token)


@router.post("/auth/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    email = normalize_email(str(payload.email))
    acquire_login_attempt_lock(db, email, request)
    enforce_login_rate_limit(db, email, request)
    user = db.scalar(select(User).where(User.email == email))
    password_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    password_valid = verify_password(payload.password, password_hash)
    if user is None or not password_valid:
        record_login_failure(db, email, request)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="Account disabled")
    clear_login_failures(db, email, request)
    issued = issue_authentication(db, user)
    db.commit()
    set_refresh_cookie(response, issued.refresh_token)
    return token_response(user, issued.access_token)


@router.post("/auth/refresh", response_model=TokenResponse)
def refresh_authentication(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    raw_refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_refresh_token is None:
        raise HTTPException(status_code=401, detail="Refresh token required")
    user, issued = rotate_refresh_token(db, raw_refresh_token)
    set_refresh_cookie(response, issued.refresh_token)
    return token_response(user, issued.access_token)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    revoked_session_id = logout_refresh_token(db, request.cookies.get(REFRESH_COOKIE_NAME))
    if revoked_session_id is None:
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            try:
                _user_id, access_session_id = decode_access_token(token)
            except (ValueError, TypeError, jwt.PyJWTError):
                pass
            else:
                revoke_session(db, access_session_id, reason="logout")
                db.commit()
    response = Response(status_code=204)
    delete_refresh_cookie(response)
    return response


@router.get("/auth/me", response_model=UserRead)
def get_me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post("/knowledge/search", response_model=list[KnowledgeSearchResult])
def knowledge_search(
    payload: KnowledgeSearchRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[KnowledgeSearchResult]:
    robot_model = db.scalar(select(RobotModel).where(RobotModel.id == payload.robot_model_id))
    if robot_model is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    safety_block = detect_safety_block(payload.query)
    if safety_block is not None:
        emit_json_log(
            logging.WARNING,
            "knowledge_safety_block",
            trace_id=request_trace_id(request),
            robot_model_id=payload.robot_model_id,
            category=safety_block.category,
            risk_level=safety_block.risk_level,
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SAFETY_BLOCKED",
                "blocked": True,
                "category": safety_block.category,
                "risk_level": safety_block.risk_level,
                "reason": safety_block.reason,
                "official_service_advice": safety_block.advice,
            },
        )
    settings = get_settings()
    enforce_business_rate_limit(
        db,
        request,
        action="knowledge_search",
        user_id=user.id,
        settings=settings,
    )
    threshold_version = db.scalar(
        select(KnowledgeDocument.sha256)
        .where(KnowledgeDocument.robot_model_id == payload.robot_model_id)
        .order_by(KnowledgeDocument.id.desc())
        .limit(1)
    )
    document_shas = list(
        db.scalars(
            select(KnowledgeDocument.sha256)
            .where(KnowledgeDocument.robot_model_id == payload.robot_model_id)
            .order_by(KnowledgeDocument.sha256)
        )
    )
    knowledge_version = aggregate_knowledge_version(document_shas)
    min_score = settings.knowledge_score_threshold(robot_model.code, threshold_version)
    normalized_query = normalize_knowledge_query(payload.query)
    cache_key = knowledge_cache_key(
        settings,
        model_code=robot_model.code,
        normalized_query=normalized_query,
        knowledge_version=knowledge_version,
        top_k=payload.top_k,
        min_score=min_score,
    )
    cache = request.app.state.knowledge_search_cache
    started_at = perf_counter()
    try:
        with cache.singleflight(cache_key):
            cached = cache.get(cache_key)
            if cached is not None:
                results = list(cached)
                emit_json_log(
                    logging.INFO,
                    "knowledge_search",
                    trace_id=request_trace_id(request),
                    robot_model_id=payload.robot_model_id,
                    top_k=payload.top_k,
                    min_score=min_score,
                    result_count=len(results),
                    cache_hit=True,
                    duration_ms=round((perf_counter() - started_at) * 1000, 3),
                )
                return results
            enforce_embedding_rate_limit(
                db,
                request,
                user_id=user.id,
                settings=settings,
            )
            raw_results = search_knowledge(
                db,
                robot_model_id=payload.robot_model_id,
                query=normalized_query,
                top_k=payload.top_k,
                min_score=min_score,
                provider=request.app.state.embedding_provider,
            )
            results = [
                KnowledgeSearchResult(**result.__dict__)
                for result in raw_results
            ]
            cache.set(cache_key, results)
        emit_json_log(
            logging.INFO,
            "knowledge_search",
            trace_id=request_trace_id(request),
            robot_model_id=payload.robot_model_id,
            top_k=payload.top_k,
            min_score=min_score,
            result_count=len(results),
            cache_hit=False,
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
            sources=[
                {
                    "document_title": item.document_title,
                    "document_sha256": item.document_sha256,
                    "page_number": item.page_number,
                    "score": round(item.score, 6),
                }
                for item in raw_results
            ],
        )
        return results
    except RuntimeError as exc:
        emit_json_log(
            logging.ERROR,
            "knowledge_search_failed",
            trace_id=request_trace_id(request),
            robot_model_id=payload.robot_model_id,
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="Embedding service unavailable") from exc


@router.post("/knowledge/answer", response_model=KnowledgeAnswerResponse)
def knowledge_answer(
    payload: KnowledgeAnswerRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> KnowledgeAnswerResponse:
    """检索增强回答：安全前置阻断 → 检索 → 生成（强制引用/三类拒答）→ 留痕。"""
    robot_model = db.scalar(select(RobotModel).where(RobotModel.id == payload.robot_model_id))
    if robot_model is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    safety_block = detect_safety_block(payload.query)
    if safety_block is not None:
        emit_json_log(
            logging.WARNING,
            "knowledge_safety_block",
            trace_id=request_trace_id(request),
            robot_model_id=payload.robot_model_id,
            category=safety_block.category,
            risk_level=safety_block.risk_level,
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SAFETY_BLOCKED",
                "blocked": True,
                "category": safety_block.category,
                "risk_level": safety_block.risk_level,
                "reason": safety_block.reason,
                "official_service_advice": safety_block.advice,
            },
        )
    settings = get_settings()
    enforce_business_rate_limit(
        db,
        request,
        action="knowledge_answer",
        user_id=user.id,
        settings=settings,
    )
    enforce_embedding_rate_limit(db, request, user_id=user.id, settings=settings)
    threshold_version = db.scalar(
        select(KnowledgeDocument.sha256)
        .where(KnowledgeDocument.robot_model_id == payload.robot_model_id)
        .order_by(KnowledgeDocument.id.desc())
        .limit(1)
    )
    min_score = settings.knowledge_score_threshold(robot_model.code, threshold_version)
    try:
        result = generate_answer(
            db,
            user_id=user.id,
            robot_model_id=payload.robot_model_id,
            query=normalize_knowledge_query(payload.query),
            embedding_provider=request.app.state.embedding_provider,
            generation_provider=request.app.state.generation_provider,
            top_k=payload.top_k,
            min_score=min_score,
        )
    except RuntimeError as exc:
        emit_json_log(
            logging.ERROR,
            "knowledge_answer_failed",
            trace_id=request_trace_id(request),
            robot_model_id=payload.robot_model_id,
            error_type=type(exc).__name__,
        )
        send_alert("generation_unavailable", "智能回答服务调用失败（生成模型不可用），请检查 DashScope 配置与额度")
        raise HTTPException(status_code=503, detail="Generation service unavailable") from exc
    return KnowledgeAnswerResponse(
        status=result.status,
        answer=result.answer,
        citations=[AnswerCitationRead(**item.__dict__) for item in result.citations],
        refusal_reason=result.refusal_reason,
        record_id=result.record_id,
    )


@router.get("/knowledge/status", response_model=list[KnowledgeStatusRead])
def knowledge_status(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[KnowledgeStatusRead]:
    del user
    return [KnowledgeStatusRead(**item.__dict__) for item in get_knowledge_status(db)]


@router.get("/knowledge/health", response_model=KnowledgeHealthRead)
def knowledge_health(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> KnowledgeHealthRead:
    del user
    model_health = get_knowledge_health(db)
    embedding_configured = bool(
        getattr(request.app.state, "embedding_configured", False)
    )
    knowledge_ready = bool(model_health) and all(item.ready for item in model_health)
    if not embedding_configured:
        health_status = "external_model_unavailable"
    elif not knowledge_ready:
        health_status = "knowledge_degraded"
    else:
        health_status = "normal"
    return KnowledgeHealthRead(
        status=health_status,
        ready=knowledge_ready and embedding_configured,
        embedding_configured=embedding_configured,
        models=[KnowledgeModelHealthRead(**item.__dict__) for item in model_health],
    )


@router.get("/models", response_model=list[ModelRead])
def list_models(db: Session = Depends(get_db)) -> list[RobotModel]:
    return list(db.scalars(select(RobotModel).where(RobotModel.active.is_(True)).order_by(RobotModel.code)))


def mask_email(email: str) -> str:
    local, separator, domain = email.partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"


def audit_sensitive_admin_read(
    db: Session,
    request: Request,
    admin: User,
    *,
    action: str,
    resource_type: str,
    resource_id: int,
    related_resource_ids: dict[str, int] | None = None,
) -> None:
    """Persist the access record before any sensitive response leaves the API.

    A failed audit write fails closed: the caller receives no sensitive payload.
    Only identifiers and the request trace are recorded, never resource content.
    """

    details: dict[str, object] = {"trace_id": request_trace_id(request)}
    if related_resource_ids:
        details["related_resource_ids"] = related_resource_ids
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            details_json=details,
        )
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sensitive resource access could not be audited",
        )


@router.get("/admin/overview", response_model=AdminOverviewRead)
def admin_overview(
    db: Session = Depends(get_db), admin: User = Depends(require_admin)
) -> AdminOverviewRead:
    del admin

    def count(statement) -> int:
        return int(db.scalar(statement) or 0)

    return AdminOverviewRead(
        user_count=count(select(func.count()).select_from(User)),
        active_model_count=count(
            select(func.count()).select_from(RobotModel).where(RobotModel.active.is_(True))
        ),
        published_flow_count=count(
            select(func.count())
            .select_from(DiagnosticFlow)
            .where(
                DiagnosticFlow.status == "published",
                DiagnosticFlow.active.is_(True),
            )
        ),
        knowledge_document_count=count(select(func.count()).select_from(KnowledgeDocument)),
        knowledge_chunk_count=count(select(func.count()).select_from(KnowledgeChunk)),
        safety_block_count=count(select(func.count()).select_from(SafetyBlockEvent)),
        unresolved_diagnostic_count=count(
            select(func.count())
            .select_from(DiagnosticSession)
            .where(DiagnosticSession.status == "unresolved")
        ),
        service_report_count=count(select(func.count()).select_from(ServiceReport)),
    )


@router.get("/admin/models", response_model=list[AdminModelRead])
def admin_list_models(
    db: Session = Depends(get_db), admin: User = Depends(require_admin)
) -> list[RobotModel]:
    del admin
    return list(db.scalars(select(RobotModel).order_by(RobotModel.code)))


@router.patch("/admin/models/{model_id}", response_model=AdminModelRead)
def admin_update_model(
    model_id: int,
    payload: AdminModelUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> RobotModel:
    robot_model = db.get(RobotModel, model_id)
    if robot_model is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    previous_active = robot_model.active
    robot_model.active = payload.active
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="robot_model.active_set",
            resource_type="robot_model",
            resource_id=str(robot_model.id),
            details_json={
                "code": robot_model.code,
                "previous_active": previous_active,
                "active": payload.active,
            },
        )
    )
    db.commit()
    db.refresh(robot_model)
    emit_json_log(
        logging.INFO,
        "admin_model_status_changed",
        trace_id=request_trace_id(request),
        actor_user_id=admin.id,
        robot_model_id=robot_model.id,
        previous_active=previous_active,
        active=robot_model.active,
    )
    return robot_model


@router.get("/admin/knowledge/status", response_model=list[KnowledgeStatusRead])
def admin_knowledge_status(
    db: Session = Depends(get_db), admin: User = Depends(require_admin)
) -> list[KnowledgeStatusRead]:
    del admin
    return [KnowledgeStatusRead(**item.__dict__) for item in get_knowledge_status(db)]


@router.get("/admin/safety-blocks", response_model=list[AdminSafetyBlockRead])
def admin_safety_blocks(
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[AdminSafetyBlockRead]:
    del admin
    rows = db.execute(
        select(SafetyBlockEvent, RobotModel.code)
        .join(UserDevice, UserDevice.id == SafetyBlockEvent.device_id)
        .join(RobotModel, RobotModel.id == UserDevice.robot_model_id)
        .order_by(SafetyBlockEvent.created_at.desc(), SafetyBlockEvent.id.desc())
        .limit(limit)
    ).all()
    return [
        AdminSafetyBlockRead(
            id=event.id,
            model_code=model_code,
            category=event.category,
            risk_level=event.risk_level,
            created_at=event.created_at,
        )
        for event, model_code in rows
    ]


@router.get(
    "/admin/safety-blocks/{event_id}",
    response_model=AdminSafetyBlockDetailRead,
)
def admin_safety_block_detail(
    event_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminSafetyBlockDetailRead:
    row = db.execute(
        select(SafetyBlockEvent, RobotModel.code)
        .join(UserDevice, UserDevice.id == SafetyBlockEvent.device_id)
        .join(RobotModel, RobotModel.id == UserDevice.robot_model_id)
        .where(SafetyBlockEvent.id == event_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Safety block event not found")
    event, model_code = row
    audit_sensitive_admin_read(
        db,
        request,
        admin,
        action="admin.safety_block_event.sensitive_read",
        resource_type="safety_block_event",
        resource_id=event.id,
        related_resource_ids={"user_id": event.user_id, "device_id": event.device_id},
    )
    return AdminSafetyBlockDetailRead(
        id=event.id,
        user_id=event.user_id,
        device_id=event.device_id,
        model_code=model_code,
        category=event.category,
        risk_level=event.risk_level,
        reason=event.reason,
        advice=event.advice,
        created_at=event.created_at,
    )


@router.get("/admin/unresolved-reports", response_model=list[AdminUnresolvedReportRead])
def admin_unresolved_reports(
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[AdminUnresolvedReportRead]:
    del admin
    rows = db.execute(
        select(ServiceReport, DiagnosticSession, RobotModel, User)
        .join(DiagnosticSession, DiagnosticSession.id == ServiceReport.session_id)
        .join(UserDevice, UserDevice.id == DiagnosticSession.device_id)
        .join(RobotModel, RobotModel.id == UserDevice.robot_model_id)
        .join(User, User.id == DiagnosticSession.user_id)
        .where(DiagnosticSession.status == "unresolved")
        .order_by(ServiceReport.created_at.desc(), ServiceReport.id.desc())
        .limit(limit)
    ).all()
    return [
        AdminUnresolvedReportRead(
            report=AdminReportSummary(
                id=report.id,
                report_number=report.report_number,
                created_at=report.created_at,
            ),
            diagnostic=AdminDiagnosticSummary(
                id=diagnostic.id,
                status=diagnostic.status,
                created_at=diagnostic.created_at,
            ),
            model=AdminRobotModelSummary(
                id=robot_model.id,
                code=robot_model.code,
                name=robot_model.name,
            ),
            user=AdminUserSummary(
                email_masked=mask_email(user.email),
            ),
        )
        for report, diagnostic, robot_model, user in rows
    ]


@router.get(
    "/admin/reports/{report_id}",
    response_model=AdminServiceReportDetailRead,
)
def admin_report_detail(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ServiceReport:
    report = db.get(ServiceReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Service report not found")
    audit_sensitive_admin_read(
        db,
        request,
        admin,
        action="admin.service_report.sensitive_read",
        resource_type="service_report",
        resource_id=report.id,
        related_resource_ids={"diagnostic_id": report.session_id},
    )
    return report


@router.get(
    "/admin/diagnostics/{diagnostic_id}",
    response_model=AdminDiagnosticDetailRead,
)
def admin_diagnostic_detail(
    diagnostic_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DiagnosticSession:
    diagnostic = db.get(DiagnosticSession, diagnostic_id)
    if diagnostic is None:
        raise HTTPException(status_code=404, detail="Diagnostic session not found")
    audit_sensitive_admin_read(
        db,
        request,
        admin,
        action="admin.diagnostic_session.sensitive_read",
        resource_type="diagnostic_session",
        resource_id=diagnostic.id,
        related_resource_ids={
            "user_id": diagnostic.user_id,
            "device_id": diagnostic.device_id,
            "flow_id": diagnostic.flow_id,
        },
    )
    return diagnostic


@router.get("/admin/audit-logs", response_model=list[AdminAuditLogRead])
def admin_audit_logs(
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[AuditLog]:
    del admin
    return list(
        db.scalars(
            select(AuditLog)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
    )


@router.get("/models/{model_id}/diagnostic-options", response_model=list[DiagnosticOptionRead])
def list_diagnostic_options(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[DiagnosticOptionRead]:
    del user
    model_exists = db.scalar(
        select(RobotModel.id).where(RobotModel.id == model_id, RobotModel.active.is_(True))
    )
    if model_exists is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    flows = list(
        db.scalars(
            select(DiagnosticFlow)
            .options(selectinload(DiagnosticFlow.issue_category))
            .where(
                DiagnosticFlow.robot_model_id == model_id,
                DiagnosticFlow.status == "published",
                DiagnosticFlow.active.is_(True),
            )
            .order_by(DiagnosticFlow.title)
        )
    )
    return [
        DiagnosticOptionRead(
            stable_key=flow.stable_key,
            version=flow.version,
            issue_category_code=flow.issue_category.code,
            issue_category_name=flow.issue_category.name,
            title=flow.title,
        )
        for flow in flows
    ]


@router.post("/devices", response_model=DeviceRead, status_code=201)
def create_device(
    payload: DeviceCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> UserDevice:
    robot_model = db.scalar(
        select(RobotModel).where(RobotModel.id == payload.robot_model_id, RobotModel.active.is_(True))
    )
    if robot_model is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    device = UserDevice(user_id=user.id, **payload.model_dump())
    db.add(device)
    db.commit()
    db.refresh(device)
    device.robot_model = robot_model
    return device


@router.get("/devices", response_model=list[DeviceRead])
def list_devices(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[UserDevice]:
    stmt = (
        select(UserDevice)
        .options(selectinload(UserDevice.robot_model))
        .where(UserDevice.user_id == user.id)
        .order_by(UserDevice.created_at.desc())
    )
    return list(db.scalars(stmt))


@router.get("/devices/{device_id}", response_model=DeviceRead)
def get_device(device_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> UserDevice:
    return owned_device(db, device_id, user)


@router.patch("/devices/{device_id}", response_model=DeviceRead)
def update_device(
    device_id: int,
    payload: DeviceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> UserDevice:
    device = owned_device(db, device_id, user)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(device, key, value)
    db.commit()
    db.refresh(device)
    return device


@router.delete("/devices/{device_id}", status_code=204)
def delete_device(
    device_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    device = owned_device(db, device_id, user)
    diagnostic_count = db.scalar(
        select(func.count()).select_from(DiagnosticSession).where(DiagnosticSession.device_id == device.id)
    )
    if diagnostic_count:
        raise HTTPException(status_code=409, detail="Device with diagnostic history cannot be deleted")
    db.delete(device)
    db.commit()
    return Response(status_code=204)


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
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SAFETY_BLOCKED",
                "blocked": True,
                "category": safety_block.category,
                "risk_level": safety_block.risk_level,
                "reason": safety_block.reason,
                "official_service_advice": safety_block.advice,
            },
        )
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
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[DiagnosticSession]:
    stmt = (
        select(DiagnosticSession)
        .options(
            selectinload(DiagnosticSession.executions).selectinload(StepExecution.step),
            selectinload(DiagnosticSession.report),
        )
        .where(DiagnosticSession.user_id == user.id)
        .order_by(DiagnosticSession.created_at.desc())
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
    lines.extend(
        [
            "最终结果：自助排查未解决，建议联系海尔官方售后。",
            "免责声明：本报告由独立第三方工具生成，不代表海尔官方诊断结论。",
        ]
    )
    return "\n".join(lines)


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
