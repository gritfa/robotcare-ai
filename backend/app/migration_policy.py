from __future__ import annotations

from typing import Any


# The HNSW index is intentionally created by the explicit migration rather
# than Base.metadata, so autogenerate/compare_metadata never tries to drop or
# recreate an index the ORM does not model.
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
