import json

import pytest
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql, sqlite

from app.db_types import EMBEDDING_DIMENSION, EmbeddingVector, normalize_embedding


def test_embedding_type_uses_text_for_sqlite_and_vector_for_postgres():
    field = EmbeddingVector()
    assert isinstance(field.load_dialect_impl(sqlite.dialect()), Text)
    postgres_type = field.load_dialect_impl(postgresql.dialect())
    assert isinstance(postgres_type, VECTOR)
    assert postgres_type.dim == EMBEDDING_DIMENSION


def test_embedding_normalization_requires_fixed_dimension():
    vector = [1.0, *([0.0] * (EMBEDDING_DIMENSION - 1))]
    assert normalize_embedding(json.dumps(vector)) == vector
    with pytest.raises(ValueError, match="exactly 256"):
        normalize_embedding([1.0, 0.0])
