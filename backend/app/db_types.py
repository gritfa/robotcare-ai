from __future__ import annotations

import json
from typing import Any

from pgvector.sqlalchemy import VECTOR
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
    """PostgreSQL 单方言：直接以 pgvector VECTOR(256) 存储嵌入向量。"""

    impl = VECTOR(EMBEDDING_DIMENSION)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect):
        return normalize_embedding(value)

    def process_result_value(self, value: Any, dialect: Dialect):
        if value is None:
            return None
        return [float(item) for item in value]
