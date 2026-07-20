from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sqlite3
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.database import build_session_factory
from app.models import KnowledgeChunk, KnowledgeDocument, RobotModel
from app.seed import seed_database


USER_DATA_TABLES = ("users", "user_devices", "diagnostic_sessions", "attachments", "service_reports")


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def count_rows(connection: sqlite3.Connection, table: str) -> int:
    if not table_exists(connection, table):
        return 0
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def migrate(source: Path) -> dict[str, int | str]:
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)

    temporary = source.with_name(f".{source.name}.alembic-migrating")
    backup = source.with_name(f"{source.name}.pre-alembic-20260720.bak")
    if temporary.exists() or backup.exists():
        raise RuntimeError("Migration temp or backup already exists; inspect it before retrying")

    source_connection = sqlite3.connect(source)
    source_connection.row_factory = sqlite3.Row
    try:
        if table_exists(source_connection, "alembic_version"):
            raise RuntimeError("Database already contains an Alembic revision")
        nonempty_user_tables = {
            table: count_rows(source_connection, table)
            for table in USER_DATA_TABLES
            if count_rows(source_connection, table)
        }
        if nonempty_user_tables:
            raise RuntimeError(
                "Legacy migration refuses databases with user data: "
                + ", ".join(f"{table}={count}" for table, count in nonempty_user_tables.items())
            )

        documents = source_connection.execute(
            """
            SELECT d.id, d.title, d.source_url, d.sha256, d.page_count, m.code AS model_code
            FROM knowledge_documents d
            JOIN robot_models m ON m.id = d.robot_model_id
            ORDER BY d.id
            """
        ).fetchall()
        chunks_by_document: dict[int, list[sqlite3.Row]] = {}
        for document in documents:
            chunks_by_document[document["id"]] = source_connection.execute(
                """
                SELECT chunk_index, page_number, content, embedding
                FROM knowledge_chunks
                WHERE document_id = ?
                ORDER BY chunk_index
                """,
                (document["id"],),
            ).fetchall()
    finally:
        source_connection.close()

    target_url = f"sqlite:///{temporary.as_posix()}"
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", target_url)
    previous_url = os.environ.get("ROBOTCARE_DATABASE_URL")
    os.environ["ROBOTCARE_DATABASE_URL"] = target_url
    try:
        command.upgrade(config, "head")
    finally:
        if previous_url is None:
            os.environ.pop("ROBOTCARE_DATABASE_URL", None)
        else:
            os.environ["ROBOTCARE_DATABASE_URL"] = previous_url

    session_factory = build_session_factory(target_url)
    target_engine = session_factory.kw["bind"]
    try:
        with session_factory() as db:
            seed_database(db)
            chunk_count = 0
            for source_document in documents:
                model = db.scalar(
                    select(RobotModel).where(RobotModel.code == source_document["model_code"])
                )
                if model is None:
                    raise RuntimeError(f"Missing seeded robot model {source_document['model_code']}")
                document = KnowledgeDocument(
                    robot_model_id=model.id,
                    title=source_document["title"],
                    source_url=source_document["source_url"],
                    sha256=source_document["sha256"],
                    page_count=source_document["page_count"],
                )
                db.add(document)
                db.flush()
                for source_chunk in chunks_by_document[source_document["id"]]:
                    db.add(
                        KnowledgeChunk(
                            document_id=document.id,
                            chunk_index=source_chunk["chunk_index"],
                            page_number=source_chunk["page_number"],
                            content=source_chunk["content"],
                            embedding=source_chunk["embedding"],
                        )
                    )
                    chunk_count += 1
            db.commit()
    finally:
        target_engine.dispose()

    shutil.copy2(source, backup)
    os.replace(temporary, source)
    return {
        "database": str(source),
        "backup": str(backup),
        "knowledge_documents": len(documents),
        "knowledge_chunks": chunk_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Move a pre-Alembic development SQLite database to the versioned schema."
    )
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    print(migrate(args.database))


if __name__ == "__main__":
    main()
