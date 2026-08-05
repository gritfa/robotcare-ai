from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import build_session_factory
from .migration_guard import ensure_database_at_head
from .models import AuditLog, AuthSession, User, utcnow
from .security import ROLE_CAPABILITIES, hash_password


ADMIN_PASSWORD_ENV = "ROBOTCARE_ADMIN_PASSWORD"


class AdminProvisioningError(RuntimeError):
    pass


def create_or_promote_admin(
    db: Session,
    *,
    email: str,
    password: str | None,
) -> tuple[User, str]:
    normalized_email = str(TypeAdapter(EmailStr).validate_python(email)).lower()
    user = db.scalar(select(User).where(User.email == normalized_email))
    if user is None:
        if not password:
            raise AdminProvisioningError(
                f"{ADMIN_PASSWORD_ENV} is required when creating a new administrator"
            )
        if not 8 <= len(password) <= 128:
            raise AdminProvisioningError("Administrator password must contain 8 to 128 characters")
        user = User(
            email=normalized_email,
            password_hash=hash_password(password),
            role="admin",
        )
        db.add(user)
        db.flush()
        db.add(
            AuditLog(
                actor_user_id=None,
                action="admin_user.created",
                resource_type="user",
                resource_id=str(user.id),
                details_json={"new_role": "admin", "source": "admin_cli"},
            )
        )
        result = "created"
    elif user.role != "admin":
        previous_role = user.role
        user.role = "admin"
        db.add(
            AuditLog(
                actor_user_id=None,
                action="admin_user.promoted",
                resource_type="user",
                resource_id=str(user.id),
                details_json={
                    "previous_role": previous_role,
                    "new_role": "admin",
                    "source": "admin_cli",
                },
            )
        )
        result = "promoted"
    else:
        result = "unchanged"

    db.commit()
    db.refresh(user)
    return user, result


def reset_password(db: Session, *, email: str, password: str | None) -> User:
    """重置已有账号的密码。

    体检发现（2026-08-05）：create 对已存在的 admin 返回 unchanged，
    管理员密码丢了 CLI **救不回来**，只能手改数据库。一个自称可运维的系统
    不该在"忘记密码"这种日常事件上要求人去写 SQL。

    重置密码同时吊销该用户的全部会话——密码之所以要重置，通常正是因为
    怀疑它已泄漏，留着旧会话等于没重置。
    """
    normalized_email = str(TypeAdapter(EmailStr).validate_python(email)).lower()
    user = db.scalar(select(User).where(User.email == normalized_email))
    if user is None:
        raise AdminProvisioningError(f"No user found for {normalized_email}")
    if not password:
        raise AdminProvisioningError(f"{ADMIN_PASSWORD_ENV} is required to reset a password")
    if not 8 <= len(password) <= 128:
        raise AdminProvisioningError("Password must contain 8 to 128 characters")

    user.password_hash = hash_password(password)
    revoked = 0
    for session in db.scalars(
        select(AuthSession).where(
            AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)
        )
    ).all():
        session.revoked_at = utcnow()
        session.revoke_reason = "password_reset"
        revoked += 1
    db.add(
        AuditLog(
            actor_user_id=None,
            action="admin_user.password_reset",
            resource_type="user",
            resource_id=str(user.id),
            details_json={"source": "admin_cli", "revoked_sessions": revoked},
        )
    )
    db.commit()
    db.refresh(user)
    return user


def set_role(db: Session, *, email: str, role: str) -> User:
    """调整角色。viewer 只读运营数据，operator 可管知识库内容，admin 全权。"""
    if role not in ROLE_CAPABILITIES:
        raise AdminProvisioningError(
            f"Unknown role {role!r}; expected one of {', '.join(sorted(ROLE_CAPABILITIES))}"
        )
    normalized_email = str(TypeAdapter(EmailStr).validate_python(email)).lower()
    user = db.scalar(select(User).where(User.email == normalized_email))
    if user is None:
        raise AdminProvisioningError(f"No user found for {normalized_email}")
    previous_role = user.role
    if previous_role == role:
        return user
    user.role = role
    db.add(
        AuditLog(
            actor_user_id=None,
            action="admin_user.role_changed",
            resource_type="user",
            resource_id=str(user.id),
            details_json={"previous_role": previous_role, "new_role": role, "source": "admin_cli"},
        )
    )
    db.commit()
    db.refresh(user)
    return user


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision a RobotCare administrator without placing passwords on the command line",
        epilog=f"For a new user, set {ADMIN_PASSWORD_ENV} in the process environment.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser(
        "create",
        help="Create a new administrator or promote an existing user",
    )
    create_parser.add_argument("--email", required=True)

    reset_parser = subparsers.add_parser(
        "reset-password",
        help="Reset an existing user's password and revoke their sessions",
    )
    reset_parser.add_argument("--email", required=True)

    role_parser = subparsers.add_parser(
        "set-role",
        help="Change a user's role (user | viewer | operator | admin)",
    )
    role_parser.add_argument("--email", required=True)
    role_parser.add_argument(
        "--role", required=True, choices=sorted(ROLE_CAPABILITIES)
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = get_settings()
    session_factory = build_session_factory(settings.database_url)
    engine = session_factory.kw["bind"]
    try:
        ensure_database_at_head(engine)
        with session_factory() as db:
            try:
                if args.command == "create":
                    user, result = create_or_promote_admin(
                        db,
                        email=args.email,
                        password=os.getenv(ADMIN_PASSWORD_ENV),
                    )
                    message = f"Administrator provisioning result: {result}; user_id={user.id}"
                elif args.command == "reset-password":
                    user = reset_password(
                        db, email=args.email, password=os.getenv(ADMIN_PASSWORD_ENV)
                    )
                    message = (
                        f"Password reset for {args.email}; user_id={user.id}; "
                        "all existing sessions revoked"
                    )
                elif args.command == "set-role":
                    user = set_role(db, email=args.email, role=args.role)
                    message = f"Role for {args.email} is now {user.role}; user_id={user.id}"
                else:
                    parser.error("unsupported command")
            except (AdminProvisioningError, ValidationError) as exc:
                parser.error(str(exc))
        print(message)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
