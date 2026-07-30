from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import DiagnosticSession, ServiceReport


def _lock_diagnostic_for_report(
    db: Session, diagnostic: DiagnosticSession
) -> DiagnosticSession:
    statement = (
        select(DiagnosticSession)
        .where(DiagnosticSession.id == diagnostic.id)
        .with_for_update()
    )
    return db.scalar(statement) or diagnostic


def get_or_create_service_report(
    db: Session,
    diagnostic: DiagnosticSession,
    content_factory: Callable[[DiagnosticSession], str],
) -> ServiceReport:
    """Return exactly one report per diagnostic under concurrent requests.

    The PostgreSQL row lock on the diagnostic serializes report creation
    across every process until this transaction commits or rolls back.
    """

    current = _lock_diagnostic_for_report(db, diagnostic)
    if current.status != "unresolved":
        raise HTTPException(
            status_code=409,
            detail="Report is available only after all steps fail",
        )

    existing = db.scalar(
        select(ServiceReport).where(ServiceReport.session_id == current.id)
    )
    if existing is not None:
        return existing

    report = ServiceReport(
        session_id=current.id,
        report_number=f"RC-{current.id:06d}-{uuid4().hex[:8].upper()}",
        content=content_factory(current),
    )
    try:
        db.add(report)
        db.commit()
    except IntegrityError:
        # The unique session_id constraint is the final cross-process
        # idempotency guard. The winning transaction may have committed
        # while this request was waiting.
        db.rollback()
        existing = db.scalar(
            select(ServiceReport).where(ServiceReport.session_id == current.id)
        )
        if existing is None:
            raise
        return existing

    db.refresh(report)
    return report
