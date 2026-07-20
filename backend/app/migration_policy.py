from __future__ import annotations

from typing import Any


# This PostgreSQL-only HNSW index is intentionally created by the explicit
# migration rather than Base.metadata, because SQLite remains the lightweight
# development/test dialect and must not receive a meaningless vector index.
MIGRATION_MANAGED_INDEXES = {"ix_knowledge_chunks_embedding_hnsw"}


def include_migration_object(
    obj: Any,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    del obj, compare_to
    if type_ == "index" and reflected and name in MIGRATION_MANAGED_INDEXES:
        return False
    return True
