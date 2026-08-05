"""引用可核验：只取命中的那一页，而不是让用户下整本 PDF。

体检发现（2026-08-05）：证据抽屉里"打开说明书原页"跳的是官方外链，
海尔那份是 27MB 整本 PDF——手机用户要下完整本再自己翻到第 15 页。
引用不可核验，"每条回答都标注资料页码"就只剩一个角标。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.knowledge_service import HashingNgramEmbeddingProvider, ingest_pdf
from app.models import KnowledgeDocument, RobotModel
from conftest import auth, register
from test_generation_service import SYNTHETIC_DIR


def _ingest(client, code: str = "RC-S200") -> tuple[int, str]:
    """入库一份带存档的文档，返回 (型号 id, sha256)。"""
    with client.app.state.session_factory() as db:
        model_id = db.scalar(select(RobotModel.id).where(RobotModel.code == code))
        source = SYNTHETIC_DIR / f"{code}_manual.pdf"
        stored_dir = Path(client.app.state.knowledge_dir)
        stored_dir.mkdir(parents=True, exist_ok=True)
        result = ingest_pdf(
            db,
            robot_model_id=model_id,
            pdf_path=source,
            source_url=f"synthetic://robotcare-demo/{code.lower()}/manual",
            provider=HashingNgramEmbeddingProvider(),
        )
        document = db.get(KnowledgeDocument, result.document_id)
        # 存档由上传接口负责；这里直接放置原件模拟已有存档
        stored_name = f"{document.sha256}.pdf"
        (stored_dir / stored_name).write_bytes(source.read_bytes())
        document.stored_filename = stored_name
        sha = document.sha256
        db.commit()
        return model_id, sha


def test_returns_single_page_pdf(client):
    _model_id, sha = _ingest(client)
    token = register(client, "citation-reader@example.com")["access_token"]

    response = client.get(f"/api/v1/knowledge/citations/{sha}/pages/1", headers=auth(token))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "inline" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-")
    # 单页应远小于原件——否则等于没解决"下整本"的问题
    original_size = (SYNTHETIC_DIR / "RC-S200_manual.pdf").stat().st_size
    assert len(response.content) < original_size


def test_requires_authentication(client):
    _model_id, sha = _ingest(client)

    response = client.get(f"/api/v1/knowledge/citations/{sha}/pages/1")

    assert response.status_code in (401, 403)


def test_unknown_sha_returns_404(client):
    token = register(client, "citation-unknown@example.com")["access_token"]

    response = client.get(
        f"/api/v1/knowledge/citations/{'a' * 64}/pages/1", headers=auth(token)
    )

    assert response.status_code == 404


def test_out_of_range_page_returns_404(client):
    _model_id, sha = _ingest(client)
    token = register(client, "citation-range@example.com")["access_token"]

    response = client.get(f"/api/v1/knowledge/citations/{sha}/pages/9999", headers=auth(token))

    assert response.status_code == 404


def test_page_zero_is_rejected(client):
    _model_id, sha = _ingest(client)
    token = register(client, "citation-zero@example.com")["access_token"]

    assert client.get(
        f"/api/v1/knowledge/citations/{sha}/pages/0", headers=auth(token)
    ).status_code == 422


def test_disabled_document_is_not_readable(client):
    """停用的文档不再参与回答，它的原件也不该继续可读。"""
    _model_id, sha = _ingest(client)
    token = register(client, "citation-disabled@example.com")["access_token"]
    with client.app.state.session_factory() as db:
        document = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.sha256 == sha))
        document.status = "disabled"
        db.commit()

    response = client.get(f"/api/v1/knowledge/citations/{sha}/pages/1", headers=auth(token))

    assert response.status_code == 404


def test_document_of_deactivated_model_is_not_readable(client):
    model_id, sha = _ingest(client)
    token = register(client, "citation-model-off@example.com")["access_token"]
    with client.app.state.session_factory() as db:
        model = db.get(RobotModel, model_id)
        model.active = False
        db.commit()

    response = client.get(f"/api/v1/knowledge/citations/{sha}/pages/1", headers=auth(token))

    assert response.status_code == 404


def test_document_without_archive_falls_back_with_clear_message(client):
    """存档功能上线前入库的老文档没有原件，要给前端一个能回退的明确信号。"""
    _model_id, sha = _ingest(client)
    token = register(client, "citation-no-archive@example.com")["access_token"]
    with client.app.state.session_factory() as db:
        document = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.sha256 == sha))
        document.stored_filename = None
        db.commit()

    response = client.get(f"/api/v1/knowledge/citations/{sha}/pages/1", headers=auth(token))

    assert response.status_code == 404
    assert "原件" in response.json()["detail"]
