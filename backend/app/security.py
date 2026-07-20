from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import AuthSession, User

password_hasher = PasswordHasher()
bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def create_access_token(
    user_id: int,
    *,
    session_id: str,
    secret: str | None = None,
    minutes: int | None = None,
) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(minutes=minutes or settings.access_token_minutes)
    return jwt.encode(
        {
            "sub": str(user_id),
            "sid": session_id,
            "type": "access",
            "iat": now,
            "exp": expiry,
        },
        secret or settings.jwt_secret,
        algorithm="HS256",
    )


def decode_access_token(token: str, secret: str | None = None) -> tuple[int, str]:
    settings = get_settings()
    payload = jwt.decode(token, secret or settings.jwt_secret, algorithms=["HS256"])
    if payload.get("type") != "access":
        raise ValueError("wrong token type")
    return int(payload["sub"]), str(payload["sid"])


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    settings = get_settings()
    try:
        user_id, session_id = decode_access_token(credentials.credentials, settings.jwt_secret)
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from None
    user = db.scalar(select(User).where(User.id == user_id))
    auth_session = db.scalar(
        select(AuthSession).where(
            AuthSession.id == session_id,
            AuthSession.user_id == user_id,
        )
    )
    now = datetime.now(timezone.utc)
    if user is None or auth_session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")
    session_expiry = auth_session.expires_at
    if session_expiry.tzinfo is None:
        session_expiry = session_expiry.replace(tzinfo=timezone.utc)
    if auth_session.revoked_at is not None or session_expiry <= now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication session revoked")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return user
