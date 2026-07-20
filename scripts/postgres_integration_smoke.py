"""Exercise the migrated application against a real PostgreSQL/pgvector database.

The caller must run ``alembic upgrade head`` first. This script deliberately
fails if PostgreSQL silently falls back to a text embedding column or if the
application cannot start against the migrated schema.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import create_app


def require_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def assert_writable_directory(variable: str) -> None:
    directory = Path(require_environment(variable)).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".write-probe-{uuid4().hex}"
    probe.write_bytes(b"robotcare")
    probe.unlink()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    database_url = require_environment("ROBOTCARE_DATABASE_URL")
    if not database_url.startswith("postgresql+"):
        raise AssertionError("integration test requires a PostgreSQL SQLAlchemy URL")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        dialect = connection.dialect.name
        extension_version = connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        vector_distance = connection.scalar(
            text("SELECT '[1,0,0]'::vector(3) <=> '[1,0,0]'::vector(3)")
        )
        embedding_type = connection.scalar(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "WHERE c.relname = 'knowledge_chunks' "
                "AND a.attname = 'embedding' AND NOT a.attisdropped"
            )
        )
        database_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))

    require(dialect == "postgresql", f"unexpected dialect: {dialect}")
    require(extension_version is not None, "pgvector extension is not installed")
    require(float(vector_distance) == 0.0, "pgvector distance operator returned an invalid result")
    require(str(embedding_type).startswith("vector("), f"embedding is not pgvector: {embedding_type}")

    alembic_config = Config("alembic.ini")
    migration_head = ScriptDirectory.from_config(alembic_config).get_current_head()
    require(database_revision == migration_head, "database is not at the Alembic head")

    assert_writable_directory("ROBOTCARE_ATTACHMENT_DIR")
    assert_writable_directory("ROBOTCARE_REPORT_DIR")

    app = create_app(auto_create_schema=False)
    with TestClient(app) as client:
        health = client.get("/health")
        require(health.status_code == 200, health.text)
        require(health.json() == {"status": "ok"}, "unexpected health payload")
        ready = client.get("/ready")
        require(ready.status_code == 200, ready.text)
        require(ready.json().get("status") == "ready", "application is not ready")
        require(
            all(
                component.get("status") == "ok"
                for component in ready.json().get("components", {}).values()
            ),
            "one or more readiness components failed",
        )

        models = client.get("/api/v1/models")
        require(models.status_code == 200, models.text)
        model_codes = {item["code"] for item in models.json()}
        require({"JH69U1", "VC35U1"} <= model_codes, "seeded models are missing")

        registration = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"postgres-ci-{uuid4().hex}@example.com",
                "password": "StrongPass123",
            },
        )
        require(registration.status_code == 201, registration.text)
        require(bool(registration.json().get("access_token")), "registration returned no token")

    engine.dispose()
    print(
        "postgres integration: passed "
        f"(pgvector={extension_version}, embedding={embedding_type}, revision={database_revision})"
    )


if __name__ == "__main__":
    main()
