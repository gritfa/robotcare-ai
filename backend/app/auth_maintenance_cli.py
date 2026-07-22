from __future__ import annotations

import argparse
from collections.abc import Sequence

from .auth_service import cleanup_expired_login_throttles
from .config import get_settings
from .database import build_session_factory
from .migration_guard import ensure_database_at_head
from .rate_limit_service import cleanup_expired_api_rate_limits


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RobotCare authentication maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "cleanup-login-throttles",
        help="Delete expired, inactive database login throttle buckets",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    session_factory = build_session_factory(settings.database_url)
    engine = session_factory.kw["bind"]
    ensure_database_at_head(engine)
    try:
        with session_factory() as db:
            login_deleted = cleanup_expired_login_throttles(db, settings)
            api_deleted = cleanup_expired_api_rate_limits(db)
    finally:
        engine.dispose()

    print(f"expired login throttle buckets deleted: {login_deleted}")
    print(f"expired API rate-limit buckets deleted: {api_deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
