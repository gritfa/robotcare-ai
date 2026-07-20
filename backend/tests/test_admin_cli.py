from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
import pytest

from app.admin_cli import ADMIN_PASSWORD_ENV, main
from app.config import get_settings
from app.models import AuditLog, User
from app.security import hash_password, verify_password


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_admin_cli_creates_promotes_and_is_idempotent(tmp_path, monkeypatch, capsys):
    database_url = f"sqlite:///{(tmp_path / 'admin-cli.db').as_posix()}"
    monkeypatch.delenv("ROBOTCARE_DATABASE_URL", raising=False)
    command.upgrade(alembic_config(database_url), "head")

    engine = create_engine(database_url)
    original_hash = hash_password("OriginalPass123")
    with Session(engine) as db:
        db.add(User(email="existing@example.com", password_hash=original_hash, role="user"))
        db.commit()
    engine.dispose()

    monkeypatch.setenv("ROBOTCARE_DATABASE_URL", database_url)
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, "EnvironmentOnlyPass123")
    get_settings.cache_clear()
    assert main(["create", "--email", "NEW-ADMIN@example.com"]) == 0
    first_output = capsys.readouterr().out
    assert "EnvironmentOnlyPass123" not in first_output

    monkeypatch.delenv(ADMIN_PASSWORD_ENV)
    get_settings.cache_clear()
    assert main(["create", "--email", "new-admin@example.com"]) == 0
    assert "unchanged" in capsys.readouterr().out

    get_settings.cache_clear()
    assert main(["create", "--email", "existing@example.com"]) == 0
    assert "promoted" in capsys.readouterr().out

    engine = create_engine(database_url)
    with Session(engine) as db:
        created = db.scalar(select(User).where(User.email == "new-admin@example.com"))
        assert created is not None
        assert created.role == "admin"
        assert verify_password("EnvironmentOnlyPass123", created.password_hash)

        promoted = db.scalar(select(User).where(User.email == "existing@example.com"))
        assert promoted is not None
        assert promoted.role == "admin"
        assert promoted.password_hash == original_hash

        assert db.scalar(select(func.count()).select_from(AuditLog)) == 2
        actions = set(db.scalars(select(AuditLog.action)))
        assert actions == {"admin_user.created", "admin_user.promoted"}
        for audit in db.scalars(select(AuditLog)):
            assert audit.actor_user_id is None
            serialized = str(audit.details_json).lower()
            assert "password" not in serialized
            assert "token" not in serialized
    engine.dispose()
    get_settings.cache_clear()


def test_admin_cli_requires_environment_password_for_a_new_user(tmp_path, monkeypatch, capsys):
    database_url = f"sqlite:///{(tmp_path / 'missing-password.db').as_posix()}"
    monkeypatch.delenv("ROBOTCARE_DATABASE_URL", raising=False)
    command.upgrade(alembic_config(database_url), "head")
    monkeypatch.setenv("ROBOTCARE_DATABASE_URL", database_url)
    monkeypatch.delenv(ADMIN_PASSWORD_ENV, raising=False)
    get_settings.cache_clear()

    with pytest.raises(SystemExit) as raised:
        main(["create", "--email", "new@example.com"])
    assert raised.value.code == 2
    error_output = capsys.readouterr().err
    assert ADMIN_PASSWORD_ENV in error_output
    assert "password=" not in error_output.lower()

    engine = create_engine(database_url)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0
        assert db.scalar(select(func.count()).select_from(AuditLog)) == 0
    engine.dispose()
    get_settings.cache_clear()
