from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auth_maintenance_cli import main
from app.config import get_settings
from app.models import ApiRateLimit, LoginThrottle


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_cleanup_login_throttles_command_deletes_expired_bucket(
    tmp_path, monkeypatch, capsys
):
    database_url = f"sqlite:///{(tmp_path / 'maintenance.db').as_posix()}"
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    monkeypatch.delenv("ROBOTCARE_DATABASE_URL", raising=False)
    command.upgrade(config, "head")

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
