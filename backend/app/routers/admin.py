"""/admin/* routes: overview, models, knowledge status, safety blocks, reports, audit logs."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..knowledge_service import get_knowledge_status
from ..models import (
    AuditLog,
    DiagnosticFlow,
    DiagnosticSession,
    KnowledgeChunk,
    KnowledgeDocument,
    RobotModel,
    SafetyBlockEvent,
    ServiceReport,
    User,
    UserDevice,
)
from ..observability import emit_json_log, request_trace_id
from ..schemas import (
    AdminAuditLogRead,
    AdminDiagnosticDetailRead,
    AdminDiagnosticSummary,
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
