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
# Pre-generated with the same Argon2id policy as ``password_hasher``.  Login
# attempts for unknown accounts verify against this fixed hash so that account
# existence does not decide whether the expensive password check runs.
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "d7/Pri/UUml1PUmpHup9dw$"
    "dD0k/dFVU1DtM1YCTFgThmBgClsVMGK4hguCaclX94M"
)
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


# 角色分级：此前只有 user/admin 两级，
# 给运营看一眼看板 = 同时给了删知识库、回滚版本、读任意用户报告的权限。
#
# 判权按**能力**而不是角色名：新增角色时只改这张表，不必回头逐个路由改判断。
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "user": frozenset(),
    # 只读运营：看板、反馈、会话、缺口、审计
    "viewer": frozenset({"read_operations"}),
    # 内容运营：加上传/发布/重新向量化/改标题与状态，但不含删除与回滚
    "operator": frozenset({"read_operations", "manage_knowledge"}),
    # 全权：加删除、版本回滚、型号增改
    "admin": frozenset({"read_operations", "manage_knowledge", "administer"}),
}

CAPABILITY_MESSAGES = {
    "read_operations": "需要运营查看权限（viewer 及以上）",
    "manage_knowledge": "需要知识库管理权限（operator 及以上）",
    "administer": "需要管理员权限（admin）",
}


def has_capability(role: str, capability: str) -> bool:
    return capability in ROLE_CAPABILITIES.get(role, frozenset())


def require_capability(capability: str):
    """按能力生成依赖。未知角色一律无权限（fail-closed）。"""

    def dependency(user: User = Depends(get_current_user)) -> User:
        if not has_capability(user.role, capability):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=CAPABILITY_MESSAGES.get(capability, "Administrator access required"),
            )
        return user

    return dependency


def require_admin(user: User = Depends(get_current_user)) -> User:
    """全权管理员。删除、回滚、型号增改等不可逆或影响面大的操作用它。"""
    if not has_capability(user.role, "administer"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return user


# 语义化别名，路由里读起来就知道这条要什么权限
require_operations_read = require_capability("read_operations")
require_knowledge_manage = require_capability("manage_knowledge")
