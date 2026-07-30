"""Cross-domain helpers and constants shared by the domain routers."""

import hmac
from io import BytesIO
import warnings

from fastapi import HTTPException, Request, status
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..models import (
    AuditLog,
    DiagnosticFlow,
    DiagnosticSession,
    DiagnosticStep,
    StepExecution,
    User,
    UserDevice,
)
from ..observability import request_trace_id
from ..schemas import RegisterRequest, TokenResponse

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
