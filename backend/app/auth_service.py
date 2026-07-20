from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import math
import secrets
from uuid import uuid4

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import case, delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .models import AuthSession, LoginThrottle, RefreshToken, User
from .security import create_access_token


REFRESH_COOKIE_NAME = "robotcare_refresh_token"
REFRESH_COOKIE_PATH = "/api/v1/auth"


@dataclass(frozen=True)
class IssuedAuthentication:
    access_token: str
    refresh_token: str
    session_id: str
    refresh_expires_at: datetime


@dataclass(frozen=True)
class LoginThrottleKey:
    key_hash: str
    scope: str
    email_hash: str
    client_ip_hash: str | None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def token_digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_expired(value: datetime, now: datetime) -> bool:
    return _as_utc(value) <= now


def _opaque_digest(settings: Settings, namespace: str, value: str) -> str:
    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        f"{namespace}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def client_ip(request: Request) -> str:
    # Do not trust X-Forwarded-For here: the backend may be reachable directly,
    # and accepting a caller-controlled header would let attackers rotate the
    # rate-limit key. Deployments needing the original address should terminate
    # traffic at a trusted proxy that supplies the ASGI client address.
    return request.client.host if request.client is not None else "unknown"


def login_throttle_keys(
    email: str, request: Request, settings: Settings | None = None
) -> tuple[LoginThrottleKey, LoginThrottleKey]:
    resolved_settings = settings or get_settings()
    normalized_email = normalize_email(email)
    email_hash = _opaque_digest(resolved_settings, "login-email", normalized_email)
    ip_hash = _opaque_digest(resolved_settings, "login-ip", client_ip(request))
    return (
        LoginThrottleKey(
            key_hash=_opaque_digest(resolved_settings, "login-key-email", normalized_email),
            scope="email",
            email_hash=email_hash,
            client_ip_hash=None,
        ),
        LoginThrottleKey(
            key_hash=_opaque_digest(
                resolved_settings,
                "login-key-email-ip",
                f"{normalized_email}\0{ip_hash}",
            ),
            scope="email_ip",
            email_hash=email_hash,
            client_ip_hash=ip_hash,
        ),
    )


def _retry_after_seconds(locked_until: datetime, now: datetime) -> int:
    return max(1, math.ceil((_as_utc(locked_until) - now).total_seconds()))


def enforce_login_rate_limit(
    db: Session, email: str, request: Request, settings: Settings | None = None
) -> None:
    resolved_settings = settings or get_settings()
    now = utcnow()
    key_hashes = [item.key_hash for item in login_throttle_keys(email, request, resolved_settings)]
    rows = list(
        db.scalars(select(LoginThrottle).where(LoginThrottle.key_hash.in_(key_hashes)))
    )
    active_locks = [
        row.locked_until
        for row in rows
        if row.locked_until is not None and _as_utc(row.locked_until) > now
    ]
    if active_locks:
        retry_after = max(_retry_after_seconds(value, now) for value in active_locks)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )


def acquire_login_attempt_lock(
    db: Session, email: str, settings: Settings | None = None
) -> None:
    """Serialize password checks for one normalized account.

    Atomic counters prevent lost increments, but a counter written only after
    verification cannot stop many requests that all pass the pre-check at the
    same time. Hold a transaction-scoped lock from before the limit check until
    the success/failure commit so only one password verification per account is
    admitted at a time.
    """

    resolved_settings = settings or get_settings()
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "sqlite":
        # SQLite is the local-development dialect. BEGIN IMMEDIATE serializes
        # writers before the rate-limit read; PostgreSQL remains account-scoped.
        db.execute(text("BEGIN IMMEDIATE"))
        return
    if dialect_name == "postgresql":
        digest = _opaque_digest(
            resolved_settings,
            "login-attempt-lock",
            normalize_email(email),
        )
        lock_id = int(digest[:16], 16)
        if lock_id >= 2**63:
            lock_id -= 2**64
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id},
        )
        return
    raise RuntimeError(
        f"Login attempt locking is not implemented for {dialect_name}"
    )


def record_login_failure(
    db: Session, email: str, request: Request, settings: Settings | None = None
) -> None:
    resolved_settings = settings or get_settings()
    now = utcnow()
    window = timedelta(minutes=resolved_settings.login_window_minutes)
    lock_duration = timedelta(minutes=resolved_settings.login_lock_minutes)
    locked_until_values: list[datetime] = []
    window_cutoff = now - window
    new_locked_until = now + lock_duration

    for key in login_throttle_keys(email, request, resolved_settings):
        dialect_name = db.get_bind().dialect.name
        if dialect_name == "sqlite":
            insert_statement = sqlite_insert(LoginThrottle)
        elif dialect_name == "postgresql":
            insert_statement = postgresql_insert(LoginThrottle)
        else:  # pragma: no cover - supported deployments use SQLite or PostgreSQL
            raise RuntimeError(
                f"Atomic login throttling is not implemented for {dialect_name}"
            )

        existing_window_is_current = LoginThrottle.window_started_at > window_cutoff
        next_failure_count = case(
            (
                existing_window_is_current,
                LoginThrottle.failure_count + 1,
            ),
            else_=1,
        )
        next_locked_until = case(
            (next_failure_count >= resolved_settings.login_max_failures, new_locked_until),
            (existing_window_is_current, LoginThrottle.locked_until),
            else_=None,
        )
        statement = (
            insert_statement.values(
                key_hash=key.key_hash,
                scope=key.scope,
                email_hash=key.email_hash,
                client_ip_hash=key.client_ip_hash,
                failure_count=1,
                window_started_at=now,
                locked_until=(
                    new_locked_until
                    if resolved_settings.login_max_failures <= 1
                    else None
                ),
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[LoginThrottle.key_hash],
                set_={
                    "failure_count": next_failure_count,
                    "window_started_at": case(
                        (existing_window_is_current, LoginThrottle.window_started_at),
                        else_=now,
                    ),
                    "locked_until": next_locked_until,
                    "updated_at": now,
                },
            )
            .returning(LoginThrottle.locked_until)
        )
        locked_until = db.execute(statement).scalar_one_or_none()
        if locked_until is not None and _as_utc(locked_until) > now:
            locked_until_values.append(locked_until)

    db.commit()

    if locked_until_values:
        retry_after = max(_retry_after_seconds(value, now) for value in locked_until_values)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )


def clear_login_failures(
    db: Session, email: str, request: Request, settings: Settings | None = None
) -> None:
    keys = login_throttle_keys(email, request, settings)
    email_hash = keys[0].email_hash
    # Clear every scope for this normalized email. A successful login should
    # remove both the current IP bucket and the account-wide anti-bypass bucket.
    db.execute(delete(LoginThrottle).where(LoginThrottle.email_hash == email_hash))


def _new_refresh_token() -> str:
    # token_urlsafe(48) carries 384 bits of entropy before URL-safe encoding.
    return secrets.token_urlsafe(48)


def issue_authentication(
    db: Session, user: User, settings: Settings | None = None
) -> IssuedAuthentication:
    resolved_settings = settings or get_settings()
    now = utcnow()
    refresh_expires_at = now + timedelta(days=resolved_settings.refresh_token_days)
    session_id = uuid4().hex
    raw_refresh_token = _new_refresh_token()
    db.add(
        AuthSession(
            id=session_id,
            user_id=user.id,
            created_at=now,
            expires_at=refresh_expires_at,
        )
    )
    db.add(
        RefreshToken(
            session_id=session_id,
            token_hash=token_digest(raw_refresh_token),
            created_at=now,
            expires_at=refresh_expires_at,
        )
    )
    return IssuedAuthentication(
        access_token=create_access_token(
            user.id,
            session_id=session_id,
            secret=resolved_settings.jwt_secret,
            minutes=resolved_settings.access_token_minutes,
        ),
        refresh_token=raw_refresh_token,
        session_id=session_id,
        refresh_expires_at=refresh_expires_at,
    )


def set_refresh_cookie(
    response: Response,
    raw_refresh_token: str,
    settings: Settings | None = None,
) -> None:
    resolved_settings = settings or get_settings()
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=raw_refresh_token,
        max_age=resolved_settings.refresh_token_days * 24 * 60 * 60,
        path=REFRESH_COOKIE_PATH,
        secure=resolved_settings.effective_refresh_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def delete_refresh_cookie(response: Response, settings: Settings | None = None) -> None:
    resolved_settings = settings or get_settings()
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        secure=resolved_settings.effective_refresh_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def revoke_session(
    db: Session,
    session_id: str,
    *,
    reason: str,
    now: datetime | None = None,
) -> None:
    revoked_at = now or utcnow()
    db.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=revoked_at, revoke_reason=reason)
        .execution_options(synchronize_session=False)
    )
    db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.session_id == session_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=revoked_at)
        .execution_options(synchronize_session=False)
    )


def rotate_refresh_token(
    db: Session, raw_refresh_token: str, settings: Settings | None = None
) -> tuple[User, IssuedAuthentication]:
    resolved_settings = settings or get_settings()
    now = utcnow()
    stored = db.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_digest(raw_refresh_token)
        )
    )
    if stored is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    auth_session = db.get(AuthSession, stored.session_id)
    if auth_session is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if stored.used_at is not None:
        reuse_age = now - _as_utc(stored.used_at)
        grace = timedelta(seconds=resolved_settings.refresh_reuse_grace_seconds)
        if reuse_age <= grace:
            retry_after = max(1, math.ceil((grace - reuse_age).total_seconds()))
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Refresh token rotation already in progress",
                headers={"Retry-After": str(retry_after)},
            )
        revoke_session(db, auth_session.id, reason="refresh_replay", now=now)
        db.commit()
        raise HTTPException(status_code=401, detail="Refresh token replay detected")

    if stored.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Authentication session revoked")

    if _is_expired(stored.expires_at, now) or _is_expired(auth_session.expires_at, now):
        revoke_session(db, auth_session.id, reason="expired", now=now)
        db.commit()
        raise HTTPException(status_code=401, detail="Refresh token expired")

    if auth_session.revoked_at is not None:
        revoke_session(db, auth_session.id, reason="refresh_replay", now=now)
        db.commit()
        raise HTTPException(status_code=401, detail="Authentication session revoked")

    user = db.get(User, auth_session.user_id)
    if user is None:
        revoke_session(db, auth_session.id, reason="user_missing", now=now)
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if user.status != "active":
        revoke_session(db, auth_session.id, reason="user_disabled", now=now)
        db.commit()
        raise HTTPException(status_code=403, detail="Account disabled")

    # The condition is the concurrency boundary. Exactly one request can move
    # an unused token to used/revoked, even if multiple workers read it first.
    result = db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.id == stored.id,
            RefreshToken.used_at.is_(None),
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
        .values(used_at=now, revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        # A concurrent worker may have completed the same rotation while this
        # statement waited on the row. Refresh the token before deciding
        # whether this is benign concurrency or a replay outside the grace.
        db.expire(stored)
        db.refresh(stored)
        if stored.used_at is not None:
            reuse_age = now - _as_utc(stored.used_at)
            grace = timedelta(seconds=resolved_settings.refresh_reuse_grace_seconds)
            if reuse_age <= grace:
                retry_after = max(1, math.ceil((grace - reuse_age).total_seconds()))
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Refresh token rotation already in progress",
                    headers={"Retry-After": str(retry_after)},
                )
        revoke_session(db, auth_session.id, reason="refresh_replay", now=now)
        db.commit()
        raise HTTPException(status_code=401, detail="Refresh token replay detected")

    next_raw_token = _new_refresh_token()
    db.add(
        RefreshToken(
            session_id=auth_session.id,
            token_hash=token_digest(next_raw_token),
            created_at=now,
            expires_at=auth_session.expires_at,
        )
    )
    db.commit()
    return user, IssuedAuthentication(
        access_token=create_access_token(
            user.id,
            session_id=auth_session.id,
            secret=resolved_settings.jwt_secret,
            minutes=resolved_settings.access_token_minutes,
        ),
        refresh_token=next_raw_token,
        session_id=auth_session.id,
        refresh_expires_at=auth_session.expires_at,
    )


def logout_refresh_token(db: Session, raw_refresh_token: str | None) -> str | None:
    if not raw_refresh_token:
        return None
    stored = db.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_digest(raw_refresh_token)
        )
    )
    if stored is None:
        return None
    revoke_session(db, stored.session_id, reason="logout")
    db.commit()
    return stored.session_id
