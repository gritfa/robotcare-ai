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
from .models import AuditLog, User
from .security import hash_password


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "create":
        parser.error("unsupported command")

    settings = get_settings()
    session_factory = build_session_factory(settings.database_url)
    engine = session_factory.kw["bind"]
    try:
        ensure_database_at_head(engine)
        with session_factory() as db:
            try:
                user, result = create_or_promote_admin(
                    db,
                    email=args.email,
                    password=os.getenv(ADMIN_PASSWORD_ENV),
                )
            except (AdminProvisioningError, ValidationError) as exc:
                parser.error(str(exc))
        print(f"Administrator provisioning result: {result}; user_id={user.id}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
