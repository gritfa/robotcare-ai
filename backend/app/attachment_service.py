from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Attachment, DiagnosticSession


def _lock_diagnostic_for_attachment(
    db: Session, diagnostic: DiagnosticSession
) -> DiagnosticSession:
    statement = (
        select(DiagnosticSession)
        .where(DiagnosticSession.id == diagnostic.id)
        .with_for_update()
    )
    return db.scalar(statement) or diagnostic


def create_attachment(
    db: Session,
    diagnostic: DiagnosticSession,
    attachment_dir: Path,
    *,
    original_filename: str,
    extension: str,
    content_type: str,
    content: bytes,
    maximum_per_diagnostic: int,
) -> Attachment:
    """Atomically enforce the per-diagnostic count and persist its file row.

    The PostgreSQL row lock on the diagnostic serializes concurrent uploads
    for the same diagnostic across every process until this transaction ends.
    """

    current = _lock_diagnostic_for_attachment(db, diagnostic)
    if current.status != "in_progress":
        raise HTTPException(
            status_code=409,
            detail="Attachments cannot be added after diagnosis ends",
        )

    attachment_count = db.scalar(
        select(func.count())
        .select_from(Attachment)
        .where(Attachment.session_id == current.id)
    )
    if (attachment_count or 0) >= maximum_per_diagnostic:
        raise HTTPException(
            status_code=409,
            detail="A diagnostic can contain at most five images",
        )

    stored_filename = f"{uuid4().hex}{extension}"
    target = attachment_dir / stored_filename
    attachment = Attachment(
        session_id=current.id,
        original_filename=original_filename,
        stored_filename=stored_filename,
        content_type=content_type,
        size_bytes=len(content),
    )
    try:
        target.write_bytes(content)
        db.add(attachment)
        db.commit()
        db.refresh(attachment)
    except Exception:
        db.rollback()
        target.unlink(missing_ok=True)
        raise
    return attachment
