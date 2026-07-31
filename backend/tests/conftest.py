import os
from io import BytesIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.config import Settings, get_settings
from app.main import create_app


BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def _isolate_local_environment():
    """测试全程不读仓库 .env，也不继承本机 DashScope 配置。

    否则开发机 .env 里的真实 key 会让"外部模型未配置"类用例被环境污染：
    CI 无密钥通过、有 key 的开发机失败。
    """

    original_env_file = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    saved = {
        name: os.environ.pop(name)
        for name in ("ROBOTCARE_DASHSCOPE_API_KEY", "ROBOTCARE_DASHSCOPE_BASE_URL")
        if name in os.environ
    }
    # get_settings 带 lru_cache，import 阶段可能已缓存污染实例，必须清掉。
    get_settings.cache_clear()
    yield
    Settings.model_config["env_file"] = original_env_file
    os.environ.update(saved)
    get_settings.cache_clear()

# 单方言：所有测试都跑在专用 PostgreSQL 测试容器上（pgvector/pgvector:pg16）。
TEST_DATABASE_URL = os.getenv(
    "ROBOTCARE_TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:test@127.0.0.1:55433/robotcare_test",
)


def alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _reset_schema(database_url: str) -> None:
    engine = create_engine(database_url, poolclass=NullPool)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def fresh_database(database_name: str) -> str:
    """Return the URL of an empty database on the test server, creating it on demand."""

    base = make_url(TEST_DATABASE_URL)
    admin = create_engine(base, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        exists = connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": database_name},
        )
        if not exists:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    admin.dispose()
    database_url = base.set(database=database_name).render_as_string(hide_password=False)
    _reset_schema(database_url)
    return database_url


@pytest.fixture(scope="session")
def database_schema():
    """Migrate the shared test database to head exactly once per session."""

    _reset_schema(TEST_DATABASE_URL)
    previous = os.environ.pop("ROBOTCARE_DATABASE_URL", None)
    try:
        command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
    finally:
        if previous is not None:
            os.environ["ROBOTCARE_DATABASE_URL"] = previous
    engine = create_engine(TEST_DATABASE_URL, poolclass=NullPool)
    tables = [
        name
        for name in inspect(engine).get_table_names()
        if name != "alembic_version"
    ]
    yield engine, tables
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_database(database_schema):
    """Truncate every table after each test; keeps the migrated schema in place."""

    engine, tables = database_schema
    yield
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE "
                + ", ".join(f'"{name}"' for name in tables)
                + " RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def migration_database_url(monkeypatch):
    """A dedicated empty database for migration tests (never the shared test DB)."""

    monkeypatch.delenv("ROBOTCARE_DATABASE_URL", raising=False)
    return fresh_database("robotcare_migration")


@pytest.fixture
def client(tmp_path):
    app = create_app(
        TEST_DATABASE_URL,
        attachment_dir=tmp_path / "attachments",
        report_dir=tmp_path / "reports",
        auto_create_schema=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def register(client: TestClient, email: str) -> dict:
    response = client.post("/api/v1/auth/register", json={"email": email, "password": "StrongPass123"})
    assert response.status_code == 201, response.text
    return response.json()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def submit_feedback(client: TestClient, token: str, diagnostic_id: int, outcome: str):
    step = client.get(
        f"/api/v1/diagnostics/{diagnostic_id}/steps/current", headers=auth(token)
    )
    assert step.status_code == 200, step.text
    return client.post(
        f"/api/v1/diagnostics/{diagnostic_id}/feedback",
        headers=auth(token),
        json={"step_id": step.json()["id"], "outcome": outcome},
    )


def image_bytes(image_format: str = "PNG", size: tuple[int, int] = (16, 16)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color=(32, 120, 90)).save(output, format=image_format)
    return output.getvalue()
