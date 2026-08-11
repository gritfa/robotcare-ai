"""引用可核验：只取命中的那一页，而不是让用户下整本 PDF。

问题背景：证据抽屉里“打开说明书原页”此前使用官方外链，
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


def test_corrupt_archive_returns_readable_error_not_500(client):
    """损坏/非 PDF 的原件此前让 pypdf 直接抛穿，用户看到的是裸 500。"""
    _model_id, sha = _ingest(client)
    token = register(client, "citation-corrupt@example.com")["access_token"]
    with client.app.state.session_factory() as db:
        document = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.sha256 == sha))
        stored = Path(client.app.state.knowledge_dir) / document.stored_filename
        stored.write_bytes(b"not a pdf at all")
        db.commit()

    response = client.get(f"/api/v1/knowledge/citations/{sha}/pages/1", headers=auth(token))

    assert response.status_code == 422
    assert "原件" in response.json()["detail"]


def test_second_read_is_served_from_cache(client):
    """同一 (sha, page) 结果不变，第二次不该再解析整本 PDF。"""
    _model_id, sha = _ingest(client)
    token = register(client, "citation-cache@example.com")["access_token"]
    cache = client.app.state.citation_page_cache

    first = client.get(f"/api/v1/knowledge/citations/{sha}/pages/1", headers=auth(token))
    assert first.status_code == 200
    assert cache.stats()["entries"] == 1

    # 把原件内容改坏（文件仍在，所以存在性检查照过）：
    # 若第二次仍返回同样内容，只可能是走了缓存 —— 重新解析必然 422
    with client.app.state.session_factory() as db:
        document = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.sha256 == sha))
        (Path(client.app.state.knowledge_dir) / document.stored_filename).write_bytes(b"broken")

    second = client.get(f"/api/v1/knowledge/citations/{sha}/pages/1", headers=auth(token))
    assert second.status_code == 200
    assert second.content == first.content


def test_extraction_slot_is_bounded(client):
    """并发解析闸满了要 503 排队，而不是让 N 份 27MB 同时进内存。"""
    from app.citation_page_service import CitationPageCache, CitationPageCacheFull

    cache = CitationPageCache(max_concurrent_extractions=1, acquire_timeout_seconds=0.05)
    with cache.extraction_slot():
        try:
            with cache.extraction_slot():
                raise AssertionError("second slot should not be granted")
        except CitationPageCacheFull:
            pass
    # 释放后能重新拿到
    with cache.extraction_slot():
        pass


def test_page_cache_evicts_by_entries_and_bytes():
    from app.citation_page_service import CitationPageCache

    cache = CitationPageCache(max_entries=2, max_bytes=10 * 1024 * 1024)
    cache.set("a:1", b"a" * 10)
    cache.set("b:1", b"b" * 10)
    cache.get("a:1")  # a 变成最近使用
    cache.set("c:1", b"c" * 10)
    assert cache.get("b:1") is None  # b 最久未用被淘汰
    assert cache.get("a:1") is not None
    assert cache.stats()["entries"] == 2

    tiny = CitationPageCache(max_entries=100, max_bytes=32)
    tiny.set("x:1", b"x" * 20)
    tiny.set("y:1", b"y" * 20)
    assert tiny.stats()["bytes"] <= 32
    # 单页大于整缓存预算：不存，也不能把已有条目挤光
    tiny.set("huge:1", b"z" * 999)
    assert tiny.get("huge:1") is None
    assert tiny.stats()["entries"] >= 1
