"""/admin/* routes: overview, models, knowledge status/upload, content gaps, safety blocks, reports, audit logs."""

import logging
import tempfile
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..knowledge_service import get_knowledge_status, ingest_pdf
from ..models import (
    AuditLog,
    DiagnosticFlow,
    DiagnosticSession,
    GenerationRecord,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeGapEvent,
    RobotModel,
    SafetyBlockEvent,
    ServiceReport,
    User,
    UserDevice,
    utcnow,
)
from ..observability import emit_json_log, request_trace_id
from ..schemas import (
    AdminAuditLogRead,
    AdminContentGapRead,
    AdminDiagnosticDetailRead,
    AdminDiagnosticSummary,
    AdminGenerationStatsRead,
    AdminKnowledgeUploadRead,
    AdminModelRead,
    AdminModelUpdate,
    AdminOverviewRead,
    AdminReportSummary,
    AdminRobotModelSummary,
    AdminSafetyBlockDetailRead,
    AdminSafetyBlockRead,
    AdminServiceReportDetailRead,
    AdminUnresolvedReportRead,
    AdminUserSummary,
    KnowledgeStatusRead,
)
from ..security import require_admin
from ._shared import audit_sensitive_admin_read, mask_email

router = APIRouter(prefix="/api/v1")

OPERATION_STATS_WINDOW_DAYS = 30
MAX_KNOWLEDGE_PDF_BYTES = 30 * 1024 * 1024


@router.get("/admin/overview", response_model=AdminOverviewRead)
def admin_overview(
    db: Session = Depends(get_db), admin: User = Depends(require_admin)
) -> AdminOverviewRead:
    del admin

    def count(statement) -> int:
        return int(db.scalar(statement) or 0)

    stats_since = utcnow() - timedelta(days=OPERATION_STATS_WINDOW_DAYS)
    refusal_rows = db.execute(
        select(GenerationRecord.refusal_reason, func.count())
        .where(
            GenerationRecord.status == "refused",
            GenerationRecord.created_at >= stats_since,
        )
        .group_by(GenerationRecord.refusal_reason)
    ).all()
    refusal_by_reason = {
        reason: int(reason_count) for reason, reason_count in refusal_rows if reason
    }
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
        generation_stats=AdminGenerationStatsRead(
            answered_count=count(
                select(func.count())
                .select_from(GenerationRecord)
                .where(
                    GenerationRecord.status == "answered",
                    GenerationRecord.created_at >= stats_since,
                )
            ),
            refused_count=sum(refusal_by_reason.values()),
            refusal_by_reason=refusal_by_reason,
        ),
        content_gap_count=count(
            select(func.count())
            .select_from(KnowledgeGapEvent)
            .where(KnowledgeGapEvent.created_at >= stats_since)
        ),
    )


@router.get("/admin/content-gaps", response_model=list[AdminContentGapRead])
def admin_content_gaps(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[AdminContentGapRead]:
    """内容缺口榜：按归一化查询聚合缺口事件；只返回运营聚合数据，不含用户信息。"""

    del admin
    since = utcnow() - timedelta(days=days)
    rows = db.execute(
        select(
            KnowledgeGapEvent.query_normalized,
            func.count().label("event_count"),
            func.max(KnowledgeGapEvent.created_at).label("last_seen_at"),
            func.array_agg(RobotModel.code.distinct()).label("model_codes"),
        )
        .join(RobotModel, RobotModel.id == KnowledgeGapEvent.robot_model_id)
        .where(KnowledgeGapEvent.created_at >= since)
        .group_by(KnowledgeGapEvent.query_normalized)
        .order_by(
            func.count().desc(),
            func.max(KnowledgeGapEvent.created_at).desc(),
            KnowledgeGapEvent.query_normalized,
        )
        .limit(limit)
    ).all()
    return [
        AdminContentGapRead(
            query_normalized=row.query_normalized,
            count=int(row.event_count),
            model_codes=sorted(row.model_codes),
            last_seen_at=row.last_seen_at,
        )
        for row in rows
    ]


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


@router.post(
    "/admin/knowledge/upload",
    response_model=AdminKnowledgeUploadRead,
    status_code=201,
)
def admin_knowledge_upload(
    request: Request,
    model_code: str = Form(min_length=1, max_length=50),
    source_url: str = Form(min_length=1, max_length=2000),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminKnowledgeUploadRead:
    """管理员上传 PDF 入知识库：魔数/大小/型号校验 → ingest → 审计，一个事务不留半条记录。"""

    robot_model = db.scalar(select(RobotModel).where(RobotModel.code == model_code))
    if robot_model is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    content = file.file.read(MAX_KNOWLEDGE_PDF_BYTES + 1)
    if len(content) > MAX_KNOWLEDGE_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF exceeds the 30MB upload limit")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="Uploaded file is not a PDF")

    title = Path(file.filename).stem if file.filename else None
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
        handle.write(content)
        pdf_path = Path(handle.name)
    try:
        # ingest 与审计写入同一事务：任何一步失败整体回滚，不留半条记录。
        result = ingest_pdf(
            db,
            robot_model_id=robot_model.id,
            pdf_path=pdf_path,
            source_url=source_url,
            provider=request.app.state.embedding_provider,
            title=title or None,
            commit=False,
        )
        db.add(
            AuditLog(
                actor_user_id=admin.id,
                action="admin.knowledge.upload",
                resource_type="knowledge_document",
                resource_id=str(result.document_id),
                details_json={
                    "model_code": robot_model.code,
                    "sha256": result.sha256,
                    "chunk_count": result.chunk_count,
                    "created": result.created,
                    "trace_id": request_trace_id(request),
                },
            )
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        db.rollback()
        emit_json_log(
            logging.ERROR,
            "admin_knowledge_upload_failed",
            trace_id=request_trace_id(request),
            robot_model_id=robot_model.id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="Embedding service unavailable") from exc
    finally:
        pdf_path.unlink(missing_ok=True)
    emit_json_log(
        logging.INFO,
        "admin_knowledge_uploaded",
        trace_id=request_trace_id(request),
        actor_user_id=admin.id,
        robot_model_id=robot_model.id,
        document_id=result.document_id,
        sha256=result.sha256,
        chunk_count=result.chunk_count,
        created=result.created,
    )
    return AdminKnowledgeUploadRead(
        document_id=result.document_id,
        created=result.created,
        changed=result.changed,
        chunk_count=result.chunk_count,
        sha256=result.sha256,
    )


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
