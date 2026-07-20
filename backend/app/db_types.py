from __future__ import annotations

import json
from typing import Any

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


EMBEDDING_DIMENSION = 256


def normalize_embedding(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    vector = [float(item) for item in value]
    if len(vector) != EMBEDDING_DIMENSION:
        raise ValueError(
            f"Embedding must contain exactly {EMBEDDING_DIMENSION} values; got {len(vector)}"
        )
    return vector


class EmbeddingVector(TypeDecorator):
    """Use pgvector in PostgreSQL while keeping JSON text for SQLite tests."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(VECTOR(EMBEDDING_DIMENSION))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Dialect):
        vector = normalize_embedding(value)
        if vector is None:
            return None
        if dialect.name == "postgresql":
            return vector
        return json.dumps(vector, separators=(",", ":"))

    def process_result_value(self, value: Any, dialect: Dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return [float(item) for item in value]
        return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
