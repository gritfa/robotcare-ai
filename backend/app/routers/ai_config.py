import logging
from ipaddress import ip_address

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..ai_config_service import (
    AIConfigurationError,
    apply_runtime_config,
    build_runtime_providers,
    config_status,
    effective_config,
    save_config,
    set_enabled,
)
from ..config import get_settings
from ..database import get_db
from ..models import AuditLog, User
from ..observability import emit_json_log, request_trace_id
from ..rate_limit_service import enforce_business_rate_limit, enforce_embedding_rate_limit
from ..schemas import (
    AdminAIConfigRead,
    AdminAIConfigUpdate,
    AdminAIConnectionTestRead,
    AdminAIStatusUpdate,
)
from ..security import require_admin


router = APIRouter(prefix="/api/v1")


def _secure_secret_transport(request: Request, settings) -> bool:
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client is not None else ""
    try:
        peer_address = ip_address(peer)
        trusted_peer = any(
            peer_address in network for network in settings.trusted_proxy_networks
        )
        peer_is_loopback = peer_address.is_loopback
    except ValueError:
        trusted_peer = False
        peer_is_loopback = False
    # Only a configured trusted reverse proxy may assert the original scheme.
    # Reject comma-separated or ambiguous values instead of guessing.
    if trusted_peer and request.headers.get("x-forwarded-proto", "").strip().lower() == "https":
        return True
    # Permit direct loopback HTTP only for local development. A reverse-proxied
    # public request carries X-Forwarded-For, even when nginx itself is loopback.
    hostname = (request.url.hostname or "").lower()
    return bool(
        settings.environment.strip().lower() != "production"
        and not request.headers.get("x-forwarded-for")
        and hostname in {"localhost", "127.0.0.1", "::1"}
        and peer_is_loopback
    )


def _audit(db: Session, admin: User, action: str, details: dict[str, object]) -> None:
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action=action,
            resource_type="ai_provider_config",
            resource_id="1",
            details_json=details,
        )
    )


@router.get("/admin/ai-config", response_model=AdminAIConfigRead)
def read_ai_config(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminAIConfigRead:
    del admin
    return AdminAIConfigRead(
        **config_status(
            db,
            get_settings(),
            runtime_ready=bool(getattr(request.app.state, "ai_runtime_ready", False)),
        )
    )


@router.put("/admin/ai-config", response_model=AdminAIConfigRead)
def update_ai_config(
    payload: AdminAIConfigUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminAIConfigRead:
    settings = get_settings()
    submits_secret = any(
        secret is not None and bool(secret.get_secret_value().strip())
        for secret in (payload.generation_api_key, payload.embedding_api_key)
    )
    if submits_secret and not _secure_secret_transport(request, settings):
        raise HTTPException(
            status_code=403,
            detail="只允许通过 HTTPS 提交 API Key",
        )
    try:
        row = save_config(db, payload, settings, admin_id=admin.id)
        _audit(
            db,
            admin,
            "ai_config.update",
            {
                "enabled": row.enabled,
                "llm_backend": row.llm_backend,
                "generation_model": row.generation_model,
                "generation_key_replaced": payload.generation_api_key is not None,
                "embedding_key_replaced": bool(
                    payload.embedding_api_key is not None
                    or payload.reuse_generation_key_for_embedding
                ),
            },
        )
        db.commit()
        if row.enabled:
            apply_runtime_config(request.app, db, settings)
    except AIConfigurationError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AdminAIConfigRead(
        **config_status(
            db,
            settings,
            runtime_ready=bool(getattr(request.app.state, "ai_runtime_ready", False)),
        )
    )


@router.patch("/admin/ai-config/status", response_model=AdminAIConfigRead)
def update_ai_status(
    payload: AdminAIStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminAIConfigRead:
    settings = get_settings()
    try:
        set_enabled(db, payload.enabled, settings, admin_id=admin.id)
        _audit(db, admin, "ai_config.enable" if payload.enabled else "ai_config.disable", {})
        db.commit()
        apply_runtime_config(request.app, db, settings)
    except AIConfigurationError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AdminAIConfigRead(
        **config_status(
            db,
            settings,
            runtime_ready=bool(getattr(request.app.state, "ai_runtime_ready", False)),
        )
    )


@router.post("/admin/ai-config/test", response_model=AdminAIConnectionTestRead)
def test_ai_config(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminAIConnectionTestRead:
    settings = get_settings()
    enforce_embedding_rate_limit(db, request, user_id=admin.id, settings=settings)
    enforce_business_rate_limit(
        db,
        request,
        action="knowledge_answer",
        user_id=admin.id,
        settings=settings,
    )
    errors: list[str] = []
    embedding_ok = False
    generation_ok = False
    try:
        config = effective_config(db, settings, ignore_enabled=True)
        embedding_provider, generation_provider = build_runtime_providers(config, settings)
        embedding_ok = bool(embedding_provider.embed_query("连接测试"))
        generation_ok = bool(
            generation_provider.generate(
                system="你是连接测试程序。", prompt="请只回复 OK。"
            ).strip()
        )
    except AIConfigurationError as exc:
        errors.append(str(exc))
    except Exception as exc:  # noqa: BLE001 - 对外只返回脱敏错误
        errors.append("外部模型连接失败，请检查密钥、模型名称、Base URL 与账户额度")
        emit_json_log(
            logging.ERROR,
            "ai_config_test_failed",
            trace_id=request_trace_id(request),
            error_type=type(exc).__name__,
        )
    _audit(
        db,
        admin,
        "ai_config.test",
        {"embedding_service": embedding_ok, "generation_service": generation_ok},
    )
    db.commit()
    return AdminAIConnectionTestRead(
        generation_service=generation_ok,
        embedding_service=embedding_ok,
        errors=errors,
    )
