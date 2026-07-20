import json
import pytest

from pypdf import PdfWriter
from sqlalchemy import func, select

from app.knowledge_service import (
    PageText,
    extract_pdf_pages,
    get_knowledge_status,
    ingest_pdf,
    search_knowledge,
    postgres_search_statement,
    split_pages,
)
from app.models import KnowledgeChunk, KnowledgeDocument, RobotModel
from conftest import auth, register


class FakeEmbeddingProvider:
    def __init__(self, query_vector=None):
        self.document_batches: list[list[str]] = []
        self.query_vector = query_vector or vector(1.0)

    def embed_documents(self, texts):
        self.document_batches.append(list(texts))
        return [vector(float(len(text)), 1.0) for text in texts]

    def embed_query(self, text):
        return list(self.query_vector)


def vector(first: float, second: float = 0.0) -> list[float]:
    return [first, second, *([0.0] * 254)]


def model_id(db, code: str) -> int:
    return db.scalar(select(RobotModel.id).where(RobotModel.code == code))


def test_pdf_page_extraction_and_page_scoped_chunking(tmp_path):
    pdf_path = tmp_path / "blank-pages.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    with pdf_path.open("wb") as output:
        writer.write(output)

    pages, _ = extract_pdf_pages(pdf_path)
    assert [page.page_number for page in pages] == [1, 2]

    chunks = split_pages([PageText(1, "A" * 600), PageText(2, "B" * 20)])
    assert [(chunk.page_number, len(chunk.content)) for chunk in chunks] == [
        (1, 500),
        (1, 180),
        (2, 20),
    ]
    assert chunks[0].content[-80:] == chunks[1].content[:80]


def test_ingest_is_idempotent_and_changed_source_replaces_chunks(client, tmp_path, monkeypatch):
    pdf_path = tmp_path / "manual.pdf"
    pdf_path.write_bytes(b"version-one")
    provider = FakeEmbeddingProvider()

    def fake_extract(path):
        if path.read_bytes() == b"version-one":
            return [PageText(index, f"Page {index}") for index in range(1, 13)], "Manual title"
        return [PageText(1, "replacement")], "Updated title"

    monkeypatch.setattr("app.knowledge_service.extract_pdf_pages", fake_extract)
    with client.app.state.session_factory() as db:
        robot_model_id = model_id(db, "JH69U1")
        first = ingest_pdf(
            db,
            robot_model_id=robot_model_id,
            pdf_path=pdf_path,
            source_url="https://example.com/manual.pdf",
            provider=provider,
        )
        assert first.created is True
        assert first.changed is False
        assert first.chunk_count == 12
        assert [len(batch) for batch in provider.document_batches] == [10, 2]

        calls_after_first = len(provider.document_batches)
        repeated = ingest_pdf(
            db,
            robot_model_id=robot_model_id,
            pdf_path=pdf_path,
            source_url="https://example.com/manual.pdf",
            provider=provider,
        )
        assert repeated.document_id == first.document_id
        assert repeated.changed is False
        assert len(provider.document_batches) == calls_after_first

        pdf_path.write_bytes(b"version-two")
        replaced = ingest_pdf(
            db,
            robot_model_id=robot_model_id,
            pdf_path=pdf_path,
            source_url="https://example.com/manual.pdf",
            provider=provider,
        )
        assert replaced.document_id == first.document_id
        assert replaced.changed is True
        assert replaced.chunk_count == 1
        assert db.scalar(select(func.count()).select_from(KnowledgeDocument)) == 1
        stored_chunks = list(db.scalars(select(KnowledgeChunk)))
        assert len(stored_chunks) == 1
        assert stored_chunks[0].content == "replacement"


def test_search_is_model_isolated_sorted_and_thresholded(client):
    with client.app.state.session_factory() as db:
        first_model = model_id(db, "JH69U1")
        second_model = model_id(db, "VC35U1")
        first_doc = KnowledgeDocument(
            robot_model_id=first_model,
            title="JH69U1 manual",
            source_url="https://example.com/jh69u1",
            sha256="a" * 64,
            page_count=1,
        )
        other_doc = KnowledgeDocument(
            robot_model_id=second_model,
            title="VC35U1 manual",
            source_url="https://example.com/vc35u1",
            sha256="b" * 64,
            page_count=1,
        )
        db.add_all([first_doc, other_doc])
        db.flush()
        db.add_all(
            [
                KnowledgeChunk(
                    document_id=first_doc.id,
                    chunk_index=0,
                    page_number=1,
                    content="best",
                    embedding=json.dumps(vector(1.0)),
                ),
                KnowledgeChunk(
                    document_id=first_doc.id,
                    chunk_index=1,
                    page_number=1,
                    content="second",
                    embedding=json.dumps(vector(0.8, 0.2)),
                ),
                KnowledgeChunk(
                    document_id=first_doc.id,
                    chunk_index=2,
                    page_number=1,
                    content="below threshold",
                    embedding=json.dumps(vector(-1.0)),
                ),
                KnowledgeChunk(
                    document_id=other_doc.id,
                    chunk_index=0,
                    page_number=1,
                    content="must not leak",
                    embedding=json.dumps(vector(1.0)),
                ),
            ]
        )
        db.commit()

        results = search_knowledge(
            db,
            robot_model_id=first_model,
            query="charging",
            top_k=10,
            min_score=0.25,
            provider=FakeEmbeddingProvider(),
        )
        assert [result.content for result in results] == ["best", "second"]
        assert results[0].score > results[1].score
        assert all(result.document_title == "JH69U1 manual" for result in results)


def test_knowledge_api_requires_auth_and_returns_real_sources_and_status(client):
    provider = FakeEmbeddingProvider()
    client.app.state.embedding_provider = provider
    with client.app.state.session_factory() as db:
        robot_model_id = model_id(db, "JH69U1")
        document = KnowledgeDocument(
            robot_model_id=robot_model_id,
            title="Official manual",
            source_url="https://example.com/official.pdf",
            sha256="c" * 64,
            page_count=2,
        )
        db.add(document)
        db.flush()
        db.add(
            KnowledgeChunk(
                document_id=document.id,
                chunk_index=0,
                page_number=2,
                content="Clean the charging contacts.",
                embedding=json.dumps(vector(1.0)),
            )
        )
        db.commit()

    payload = {"robot_model_id": robot_model_id, "query": "cannot charge", "top_k": 3}
    assert client.post("/api/v1/knowledge/search", json=payload).status_code == 401

    token = register(client, "knowledge@example.com")["access_token"]
    response = client.post("/api/v1/knowledge/search", headers=auth(token), json=payload)
    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "score": 1.0,
            "content": "Clean the charging contacts.",
            "document_title": "Official manual",
            "source_url": "https://example.com/official.pdf",
            "page_number": 2,
        }
    ]
    assert client.get("/api/v1/knowledge/status").status_code == 401
    status_response = client.get("/api/v1/knowledge/status", headers=auth(token))
    assert status_response.status_code == 200
    status_by_code = {item["model_code"]: item for item in status_response.json()}
    assert status_by_code["JH69U1"] == {
        "robot_model_id": robot_model_id,
        "model_code": "JH69U1",
        "document_count": 1,
        "chunk_count": 1,
        "vector_count": 1,
    }
    assert status_by_code["VC35U1"]["vector_count"] == 0

    with client.app.state.session_factory() as db:
        status = {item.model_code: item for item in get_knowledge_status(db)}
        assert status["JH69U1"].document_count == 1


def test_failed_replacement_rolls_back_and_preserves_previous_chunks(client, tmp_path, monkeypatch):
    pdf_path = tmp_path / "atomic-manual.pdf"
    pdf_path.write_bytes(b"old-version")
    provider = FakeEmbeddingProvider()

    def fake_extract(path):
        content = "old approved content" if path.read_bytes() == b"old-version" else "new content"
        return [PageText(1, content)], "Atomic manual"

    monkeypatch.setattr("app.knowledge_service.extract_pdf_pages", fake_extract)
    with client.app.state.session_factory() as db:
        robot_model_id = model_id(db, "JH69U1")
        original = ingest_pdf(
            db,
            robot_model_id=robot_model_id,
            pdf_path=pdf_path,
            source_url="https://example.com/atomic.pdf",
            provider=provider,
        )
        original_sha = original.sha256
        pdf_path.write_bytes(b"new-version")

        real_commit = db.commit

        def fail_commit():
            raise RuntimeError("simulated database failure")

        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="simulated database failure"):
            ingest_pdf(
                db,
                robot_model_id=robot_model_id,
                pdf_path=pdf_path,
                source_url="https://example.com/atomic.pdf",
                provider=provider,
            )
        monkeypatch.setattr(db, "commit", real_commit)
        db.expire_all()

        document = db.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.id == original.document_id)
        )
        chunks = list(
            db.scalars(
                select(KnowledgeChunk).where(KnowledgeChunk.document_id == original.document_id)
            )
        )
        assert document is not None
        assert document.sha256 == original_sha
        assert [chunk.content for chunk in chunks] == ["old approved content"]


def test_postgres_statement_uses_cosine_top_k_and_model_filter():
    from sqlalchemy.dialects import postgresql

    statement = postgres_search_statement(
        robot_model_id=7,
        query_vector=vector(1.0),
        top_k=5,
        min_score=0.25,
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "<=>" in sql
    assert "CAST(knowledge_chunks.embedding" not in sql
    assert "robot_model_id" in sql
    assert "ORDER BY" in sql
    assert "LIMIT" in sql
