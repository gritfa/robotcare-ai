from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine


def alembic_config() -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    return config


def ensure_database_at_head(engine: Engine) -> None:
    expected = ScriptDirectory.from_config(alembic_config()).get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    if current != expected:
        raise RuntimeError(
            "Database schema is not at the required Alembic revision. "
            f"current={current or 'unversioned'}, expected={expected}. "
            "Run 'alembic upgrade head' before starting the API."
        )
