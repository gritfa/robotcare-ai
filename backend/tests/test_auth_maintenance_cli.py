from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auth_maintenance_cli import main
from app.config import get_settings
from app.models import ApiRateLimit, LoginThrottle
from conftest import TEST_DATABASE_URL


def test_cleanup_login_throttles_command_deletes_expired_bucket(
    monkeypatch, capsys
):
    database_url = TEST_DATABASE_URL

    now = datetime.now(timezone.utc)
    engine = create_engine(database_url)
    with Session(engine) as db:
        db.add(
            LoginThrottle(
                key_hash="a" * 64,
                scope="ip",
                email_hash=None,
                client_ip_hash="b" * 64,
                failure_count=1,
                window_started_at=now - timedelta(hours=2),
                locked_until=None,
                updated_at=now - timedelta(hours=2),
            )
        )
        db.add(
            ApiRateLimit(
                key_hash="c" * 64,
                action="knowledge_search",
                scope="user",
                principal_hash="d" * 64,
                window_kind="minute",
                window_started_at=now - timedelta(hours=2),
                expires_at=now - timedelta(hours=1),
                request_count=1,
                updated_at=now - timedelta(hours=2),
            )
        )
        db.commit()
    engine.dispose()

    monkeypatch.setenv("ROBOTCARE_DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        assert main(["cleanup-login-throttles"]) == 0
    finally:
        get_settings.cache_clear()

    assert "deleted: 1" in capsys.readouterr().out
    engine = create_engine(database_url)
    with Session(engine) as db:
        assert list(db.scalars(select(LoginThrottle))) == []
        assert list(db.scalars(select(ApiRateLimit))) == []
    engine.dispose()
