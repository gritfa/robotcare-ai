"""/admin/* routes: overview, models, knowledge status/upload, content gaps, safety blocks, reports, audit logs."""

import logging
import tempfile
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..knowledge_admin_service import gap_query_hash, store_knowledge_file
from ..knowledge_service import get_knowledge_status, ingest_pdf
from ..models import (
    AuditLog,
    ContentGapResolution,
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
    AdminModelCreate,
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


def _discard_new_archive(path: Path, created_by_this_request: bool) -> None:
    """入库失败时清理存档，但只清理本次请求新建的那个文件。

    存档按内容哈希命名，同一份 PDF 可能已被别的型号引用；
    无条件删除会把别人的原件一起带走。
    """

    if created_by_this_request:
        path.unlink(missing_ok=True)


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
            KnowledgeGapEvent.robot_model_id,
            RobotModel.code.label("model_code"),
            func.count().label("event_count"),
            func.max(KnowledgeGapEvent.created_at).label("last_seen_at"),
        )
        .join(RobotModel, RobotModel.id == KnowledgeGapEvent.robot_model_id)
        .where(KnowledgeGapEvent.created_at >= since)
        .group_by(
            KnowledgeGapEvent.query_normalized,
            KnowledgeGapEvent.robot_model_id,
            RobotModel.code,
        )
        .order_by(
            func.count().desc(),
            func.max(KnowledgeGapEvent.created_at).desc(),
            KnowledgeGapEvent.query_normalized,
        )
        .limit(limit)
    ).all()
    if not rows:
        return []

    # 一次取回这批缺口的处理状态，避免逐条查库
    hashes = [gap_query_hash(row.query_normalized) for row in rows]
    resolutions = {
        (item.robot_model_id, item.query_hash): item
        for item in db.scalars(
            select(ContentGapResolution)
            .options(selectinload(ContentGapResolution.linked_document))
            .where(ContentGapResolution.query_hash.in_(hashes))
        ).all()
    }
    items: list[AdminContentGapRead] = []
    for row in rows:
        resolution = resolutions.get((row.robot_model_id, gap_query_hash(row.query_normalized)))
        items.append(
            AdminContentGapRead(
                query_normalized=row.query_normalized,
                count=int(row.event_count),
                robot_model_id=row.robot_model_id,
                model_code=row.model_code,
                last_seen_at=row.last_seen_at,
                status=resolution.status if resolution else "open",
                linked_document_id=resolution.linked_document_id if resolution else None,
                linked_document_title=(
                    resolution.linked_document.title
                    if resolution and resolution.linked_document
                    else None
                ),
                replay_status=resolution.replay_status if resolution else None,
                replay_citation_count=resolution.replay_citation_count if resolution else None,
                replay_answer_excerpt=resolution.replay_answer_excerpt if resolution else None,
                replay_checked_at=resolution.replay_checked_at if resolution else None,
                resolved_at=resolution.resolved_at if resolution else None,
                note=resolution.note if resolution else None,
            )
        )
    return items


@router.get("/admin/models", response_model=list[AdminModelRead])
def admin_list_models(
    db: Session = Depends(get_db), admin: User = Depends(require_admin)
) -> list[RobotModel]:
    del admin
    return list(db.scalars(select(RobotModel).order_by(RobotModel.code)))


@router.post("/admin/models", response_model=AdminModelRead, status_code=201)
def admin_create_model(
    payload: AdminModelCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> RobotModel:
    """后台建型号。

    此前型号只能来自 knowledge/diagnostic_flows.json 的同步（flow_catalog.py），
    而该目录是 COPY 进镜像的——加一个型号＝改仓库 + 重 build + 重 deploy。
    对"售后主管接入自家型号"这个核心场景来说，那是个发版动作，不是运营动作。

    注意：同步逻辑只在 code 不存在时创建型号，所以这里手工建的型号不会被
    后续 seed 覆盖（由 test_manual_model_survives_catalog_sync 守住）。
    """
    code = payload.code.strip()
    existing = db.scalar(select(RobotModel).where(RobotModel.code == code))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Robot model code already exists")
    robot_model = RobotModel(
        code=code, name=payload.name.strip(), brand=payload.brand.strip(), active=True
    )
    db.add(robot_model)
    db.flush()
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="robot_model.created",
            resource_type="robot_model",
            resource_id=str(robot_model.id),
            details_json={
                "code": robot_model.code,
                "name": robot_model.name,
                "brand": robot_model.brand,
            },
        )
    )
    db.commit()
    db.refresh(robot_model)
    emit_json_log(
        logging.INFO,
        "admin_model_created",
        trace_id=request_trace_id(request),
        actor_user_id=admin.id,
        robot_model_id=robot_model.id,
        code=robot_model.code,
    )
    return robot_model


@router.patch("/admin/models/{model_id}", response_model=AdminModelRead)
def admin_update_model(
    model_id: int,
    payload: AdminModelUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> RobotModel:
    """改型号名称/品牌/启用状态。

    没有硬删除接口：型号被知识文档、用户设备、诊断流程、生成记录、会话等
    七张表引用，硬删要么被外键挡下，要么连带毁掉历史留痕。停用（active=False）
    就是这里的删除语义——用户端下拉框立刻看不到它，已有历史保持可查。
    """
    robot_model = db.get(RobotModel, model_id)
    if robot_model is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    previous = {
        "name": robot_model.name,
        "brand": robot_model.brand,
        "active": robot_model.active,
    }
    previous_active = robot_model.active
    if payload.name is not None:
        robot_model.name = payload.name.strip()
    if payload.brand is not None:
        robot_model.brand = payload.brand.strip()
    if payload.active is not None:
        robot_model.active = payload.active
    # 动作名按实际变化选：只动 active 时沿用历史动作名，避免既有审计记录
    # 与新记录语义分叉；改了名称/品牌才记 updated
    only_active_changed = payload.name is None and payload.brand is None
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="robot_model.active_set" if only_active_changed else "robot_model.updated",
            resource_type="robot_model",
            resource_id=str(robot_model.id),
            # 审计要能回答"当时改了什么"，所以前后值都记。
            # previous_active/active 是既有契约，保留不动，新字段只做增量——
            # 审计记录一旦被消费方依赖，换结构就是破坏历史。
            details_json={
                "code": robot_model.code,
                "previous_active": previous["active"],
                "active": robot_model.active,
                "previous": previous,
                "current": {
                    "name": robot_model.name,
                    "brand": robot_model.brand,
                    "active": robot_model.active,
                },
            },
        )
    )
    db.commit()
    db.refresh(robot_model)
    emit_json_log(
        logging.INFO,
        "admin_model_updated",
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
    # 先把原件存档再入库：没有原件，重新向量化/版本回滚/下载原件三件事全都做不了。
    storage_dir = Path(request.app.state.knowledge_dir)
    stored_filename, file_size, file_created = store_knowledge_file(storage_dir, content)
    pdf_path = storage_dir / stored_filename
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
            stored_filename=stored_filename,
            file_size=file_size,
            actor_user_id=admin.id,
            change_kind="upload",
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
        _discard_new_archive(pdf_path, file_created)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        db.rollback()
        _discard_new_archive(pdf_path, file_created)
        emit_json_log(
            logging.ERROR,
            "admin_knowledge_upload_failed",
            trace_id=request_trace_id(request),
            robot_model_id=robot_model.id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="Embedding service unavailable") from exc
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
