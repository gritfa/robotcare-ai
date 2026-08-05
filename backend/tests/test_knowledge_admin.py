"""知识库后台：文档生命周期（列表/分片/停用/删除/原件下载）、版本回滚、上传前差异预览、内容缺口闭环。"""

from __future__ import annotations

import sys
from pathlib import Path

from pypdf import PdfWriter
from sqlalchemy import select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.knowledge_service import HashingNgramEmbeddingProvider
from app.models import (
    AuditLog,
    ContentGapResolution,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
)
from conftest import auth, register
from test_admin_api import make_admin
from test_content_gaps import insert_gap_event, model_id_by_code

SYNTHETIC_DIR = PROJECT_ROOT / "knowledge" / "synthetic"
SOURCE_URL = "synthetic://admin/rc-s200/manual"


class ScriptedGenerationProvider:
    model_name = "scripted-test-model"

    def __init__(self, outputs):
        self.outputs = list(outputs)

    def generate(self, *, system: str, prompt: str) -> str:
        del system, prompt
        return self.outputs.pop(0)


def make_pdf(pages: list[str]) -> bytes:
    """造一份文本可提取的多页 PDF；页文本不同才能验证逐页差异。

    页面内容一律用 ASCII：reportlab 默认字体渲染中文会退化成同样的占位字形，
    各页文本哈希会变得一样，逐页差异就永远是空的——那样测的是字体不是代码。
    """

    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for text in pages:
        pdf.drawString(72, 720, text)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def upload(client, token: str, *, content: bytes, model_code: str = "RC-S200") -> dict:
    response = client.post(
        "/api/v1/admin/knowledge/upload",
        headers=auth(token),
        data={"model_code": model_code, "source_url": SOURCE_URL},
        files={"file": ("manual.pdf", content, "application/pdf")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def setup_admin(client) -> str:
    client.app.state.embedding_provider = HashingNgramEmbeddingProvider()
    return make_admin(client)


# --------------------------------------------------------------------------
# 文档列表 / 分片 / 原件
# --------------------------------------------------------------------------


def test_document_list_exposes_version_source_and_counts(client):
    token = setup_admin(client)
    body = upload(client, token, content=(SYNTHETIC_DIR / "RC-S200_manual.pdf").read_bytes())

    response = client.get("/api/v1/admin/knowledge/documents", headers=auth(token))
    assert response.status_code == 200, response.text
    items = [item for item in response.json() if item["id"] == body["document_id"]]
    assert len(items) == 1
    document = items[0]
    assert document["model_code"] == "RC-S200"
    assert document["source_url"] == SOURCE_URL
    assert len(document["sha256"]) == 64
    assert document["page_count"] > 0
    assert document["chunk_count"] == body["chunk_count"]
    assert document["vector_count"] == body["chunk_count"]
    assert document["status"] == "active"
    assert document["version"] == 1
    # 上传即存档，后续的重建/回滚/下载才有依据
    assert document["has_archived_file"] is True
    assert document["file_size"] > 0

    filtered = client.get(
        "/api/v1/admin/knowledge/documents?model_code=JH69U1", headers=auth(token)
    )
    assert all(item["model_code"] == "JH69U1" for item in filtered.json())


def test_chunk_listing_paginates_and_filters_by_page(client):
    token = setup_admin(client)
    body = upload(client, token, content=(SYNTHETIC_DIR / "RC-S200_manual.pdf").read_bytes())
    document_id = body["document_id"]

    first = client.get(
        f"/api/v1/admin/knowledge/documents/{document_id}/chunks?limit=2",
        headers=auth(token),
    )
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["total"] == body["chunk_count"]
    assert len(page["items"]) <= 2
    assert page["items"][0]["chunk_index"] == 0
    assert page["items"][0]["page_number"] >= 1
    assert page["items"][0]["content"].strip()
    assert page["items"][0]["has_embedding"] is True

    target_page = page["items"][0]["page_number"]
    scoped = client.get(
        f"/api/v1/admin/knowledge/documents/{document_id}/chunks?page_number={target_page}",
        headers=auth(token),
    )
    assert scoped.status_code == 200
    assert {item["page_number"] for item in scoped.json()["items"]} == {target_page}

    missing = client.get("/api/v1/admin/knowledge/documents/999999/chunks", headers=auth(token))
    assert missing.status_code == 404


def test_original_pdf_download_returns_bytes_and_audits(client):
    token = setup_admin(client)
    content = (SYNTHETIC_DIR / "RC-S200_manual.pdf").read_bytes()
    body = upload(client, token, content=content)

    response = client.get(
        f"/api/v1/admin/knowledge/documents/{body['document_id']}/file", headers=auth(token)
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == content  # 下载的必须是原件本身，不是重新生成的东西

    with client.app.state.session_factory() as db:
        audits = list(
            db.scalars(select(AuditLog).where(AuditLog.action == "admin.knowledge.download"))
        )
    assert len(audits) == 1


def test_document_without_archive_reports_missing_file_instead_of_pretending(client):
    """存档功能上线前入库的老文档：下载和重建都必须明确失败。"""

    token = setup_admin(client)
    body = upload(client, token, content=(SYNTHETIC_DIR / "RC-S200_manual.pdf").read_bytes())
    document_id = body["document_id"]
    with client.app.state.session_factory() as db:
        document = db.get(KnowledgeDocument, document_id)
        document.stored_filename = None
        db.commit()

    listed = client.get("/api/v1/admin/knowledge/documents", headers=auth(token)).json()
    assert next(item for item in listed if item["id"] == document_id)["has_archived_file"] is False

    download = client.get(
        f"/api/v1/admin/knowledge/documents/{document_id}/file", headers=auth(token)
    )
    assert download.status_code == 404
    reindex = client.post(
        f"/api/v1/admin/knowledge/documents/{document_id}/reindex", headers=auth(token)
    )
    assert reindex.status_code == 409
    assert "重新上传" in reindex.json()["detail"]


# --------------------------------------------------------------------------
# 停用 / 删除
# --------------------------------------------------------------------------


def test_disabling_document_removes_it_from_retrieval_but_keeps_data(client):
    token = setup_admin(client)
    user_token = register(client, "search-user@example.com")["access_token"]
    body = upload(client, token, content=(SYNTHETIC_DIR / "RC-S200_manual.pdf").read_bytes())
    document_id = body["document_id"]
    robot_model_id = model_id_by_code(client, "RC-S200")
    # 测试用的是字面 n-gram embedding，查询必须与手册原文高度重合才会命中，
    # 否则测的就不是"停用生效"而是"检索没召回"。
    query = {"robot_model_id": robot_model_id, "query": "尘盒容量 400 毫升，水箱容量 240 毫升"}

    before = client.post("/api/v1/knowledge/search", headers=auth(user_token), json=query)
    assert before.status_code == 200 and before.json(), "停用前应能检索到内容"

    disabled = client.patch(
        f"/api/v1/admin/knowledge/documents/{document_id}",
        headers=auth(token),
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"

    after = client.post("/api/v1/knowledge/search", headers=auth(user_token), json=query)
    assert after.status_code == 200
    assert after.json() == [], "停用的文档不得再参与检索"

    # 停用不是删除：分片和向量原样保留，启用后立刻恢复
    with client.app.state.session_factory() as db:
        remaining = db.scalar(
            select(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id)
        )
        assert remaining is not None

    client.patch(
        f"/api/v1/admin/knowledge/documents/{document_id}",
        headers=auth(token),
        json={"status": "active"},
    )
    restored = client.post("/api/v1/knowledge/search", headers=auth(user_token), json=query)
    assert restored.json(), "重新启用后应恢复检索"


def test_delete_document_removes_chunks_versions_and_archive(client):
    token = setup_admin(client)
    body = upload(client, token, content=make_pdf(["Page one content A", "Page two content B"]))
    document_id = body["document_id"]
    storage_dir = Path(client.app.state.knowledge_dir)
    with client.app.state.session_factory() as db:
        stored_filename = db.get(KnowledgeDocument, document_id).stored_filename
    assert (storage_dir / stored_filename).is_file()

    response = client.delete(
        f"/api/v1/admin/knowledge/documents/{document_id}", headers=auth(token)
    )
    assert response.status_code == 204, response.text

    with client.app.state.session_factory() as db:
        assert db.get(KnowledgeDocument, document_id) is None
        assert (
            db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id))
            is None
        )
        assert (
            db.scalar(
                select(KnowledgeDocumentVersion).where(
                    KnowledgeDocumentVersion.document_id == document_id
                )
            )
            is None
        )
        audits = list(
            db.scalars(select(AuditLog).where(AuditLog.action == "admin.knowledge.delete"))
        )
    assert len(audits) == 1
    assert not (storage_dir / stored_filename).exists(), "无人引用的存档应随文档一起清理"


def test_delete_keeps_archive_shared_with_another_document(client):
    """同一份 PDF 被两个型号引用时，删掉一个不能带走另一个的原件。"""

    token = setup_admin(client)
    content = make_pdf(["Shared manual content"])
    first = upload(client, token, content=content, model_code="RC-S200")
    second_response = client.post(
        "/api/v1/admin/knowledge/upload",
        headers=auth(token),
        data={"model_code": "JH69U1", "source_url": "synthetic://admin/jh69u1/shared"},
        files={"file": ("manual.pdf", content, "application/pdf")},
    )
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()

    storage_dir = Path(client.app.state.knowledge_dir)
    with client.app.state.session_factory() as db:
        stored_filename = db.get(KnowledgeDocument, first["document_id"]).stored_filename
        assert db.get(KnowledgeDocument, second["document_id"]).stored_filename == stored_filename

    client.delete(f"/api/v1/admin/knowledge/documents/{first['document_id']}", headers=auth(token))
    assert (storage_dir / stored_filename).is_file(), "另一个文档仍在引用这份存档"

    download = client.get(
        f"/api/v1/admin/knowledge/documents/{second['document_id']}/file", headers=auth(token)
    )
    assert download.status_code == 200


# --------------------------------------------------------------------------
# 版本历史 / 重建 / 回滚
# --------------------------------------------------------------------------


def test_version_history_grows_and_rollback_restores_previous_content(client):
    token = setup_admin(client)
    v1_bytes = make_pdf(["Cleaning guide revision one", "Maintenance guide page two"])
    v2_bytes = make_pdf(["Cleaning guide revision two", "Maintenance guide page two"])
    first = upload(client, token, content=v1_bytes)
    document_id = first["document_id"]
    second = upload(client, token, content=v2_bytes)
    assert second["document_id"] == document_id
    assert second["changed"] is True

    detail = client.get(
        f"/api/v1/admin/knowledge/documents/{document_id}", headers=auth(token)
    ).json()
    assert detail["version"] == 2
    assert [entry["version"] for entry in detail["versions"]] == [2, 1]
    assert detail["versions"][0]["change_kind"] == "upload"
    assert detail["sha256"] != first["sha256"]

    rollback = client.post(
        f"/api/v1/admin/knowledge/documents/{document_id}/rollback",
        headers=auth(token),
        json={"version": 1},
    )
    assert rollback.status_code == 200, rollback.text
    rolled = rollback.json()
    # 回滚恢复的是内容，不是版本号：版本号继续向前，审计链才完整
    assert rolled["sha256"] == first["sha256"]
    assert rolled["version"] == 3
    assert rolled["versions"][0]["change_kind"] == "rollback"
    assert "回滚至 v1" in rolled["versions"][0]["note"]

    download = client.get(
        f"/api/v1/admin/knowledge/documents/{document_id}/file", headers=auth(token)
    )
    assert download.content == v1_bytes

    with client.app.state.session_factory() as db:
        audits = list(
            db.scalars(select(AuditLog).where(AuditLog.action == "admin.knowledge.rollback"))
        )
    assert len(audits) == 1 and audits[0].details_json["target_version"] == 1


def test_rollback_rejects_same_content_and_unknown_version(client):
    token = setup_admin(client)
    body = upload(client, token, content=make_pdf(["Only one version"]))
    document_id = body["document_id"]

    same = client.post(
        f"/api/v1/admin/knowledge/documents/{document_id}/rollback",
        headers=auth(token),
        json={"version": 1},
    )
    assert same.status_code == 409
    missing = client.post(
        f"/api/v1/admin/knowledge/documents/{document_id}/rollback",
        headers=auth(token),
        json={"version": 99},
    )
    assert missing.status_code == 404


def test_reindex_rebuilds_vectors_from_archived_file(client):
    token = setup_admin(client)
    body = upload(client, token, content=make_pdf(["Vector rebuild test content"]))
    document_id = body["document_id"]
    # 模拟向量损坏/模型更换：清空向量后重建应恢复
    with client.app.state.session_factory() as db:
        for chunk in db.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id)
        ):
            chunk.embedding = None
        db.commit()

    listed = client.get("/api/v1/admin/knowledge/documents", headers=auth(token)).json()
    assert next(item for item in listed if item["id"] == document_id)["vector_count"] == 0

    response = client.post(
        f"/api/v1/admin/knowledge/documents/{document_id}/reindex", headers=auth(token)
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["vector_count"] == detail["chunk_count"] > 0
    assert detail["sha256"] == body["sha256"], "重建不改变内容，只重算向量"
    assert detail["versions"][0]["change_kind"] == "reindex"


# --------------------------------------------------------------------------
# 上传前差异预览
# --------------------------------------------------------------------------


def test_preview_reports_new_identical_and_page_level_changes(client):
    token = setup_admin(client)
    v1_bytes = make_pdf(["Page one original", "Page two original"])

    def preview(content: bytes) -> dict:
        response = client.post(
            "/api/v1/admin/knowledge/preview",
            headers=auth(token),
            data={"model_code": "RC-S200", "source_url": SOURCE_URL},
            files={"file": ("manual.pdf", content, "application/pdf")},
        )
        assert response.status_code == 200, response.text
        return response.json()

    fresh = preview(v1_bytes)
    assert fresh["status"] == "new"
    assert fresh["incoming_page_count"] == 2
    assert fresh["document_id"] is None

    upload(client, token, content=v1_bytes)
    # 预览不入库：连续预览两次也不会产生第二个文档
    same = preview(v1_bytes)
    assert same["status"] == "identical"
    assert same["current_version"] == 1
    assert same["page_delta"] == 0 and same["chunk_delta"] == 0

    changed = preview(make_pdf(["Page one original", "Page two modified", "Page three added"]))
    assert changed["status"] == "changed"
    assert changed["page_delta"] == 1
    assert changed["pages_comparable"] is True
    assert changed["changed_pages"] == [2]
    assert changed["added_pages"] == [3]
    assert changed["removed_pages"] == []

    with client.app.state.session_factory() as db:
        documents = list(
            db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.source_url == SOURCE_URL))
        )
    assert len(documents) == 1, "预览不得写库"


def test_preview_states_why_page_diff_is_unavailable_for_legacy_documents(client):
    token = setup_admin(client)
    upload(client, token, content=make_pdf(["Original content"]))
    with client.app.state.session_factory() as db:
        document = db.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.source_url == SOURCE_URL)
        )
        document.stored_filename = None
        db.commit()

    response = client.post(
        "/api/v1/admin/knowledge/preview",
        headers=auth(token),
        data={"model_code": "RC-S200", "source_url": SOURCE_URL},
        files={"file": ("manual.pdf", make_pdf(["Brand new content"]), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "changed"
    # 老文档没原件，逐页比对做不了——如实说明，而不是编一个看似精确的差异
    assert body["pages_comparable"] is False
    assert body["pages_incomparable_reason"]
    assert body["changed_pages"] == []


# --------------------------------------------------------------------------
# 内容缺口闭环
# --------------------------------------------------------------------------


def test_gap_closure_loop_from_link_to_replay_to_resolved(client):
    token = setup_admin(client)
    robot_model_id = model_id_by_code(client, "RC-S200")
    query = "尘盒容量 400 毫升，水箱容量 240 毫升"
    insert_gap_event(client, model_code="RC-S200", query=query)

    # 1. 缺口榜里是 open，没有任何复测痕迹
    gaps = client.get("/api/v1/admin/content-gaps", headers=auth(token)).json()
    gap = next(item for item in gaps if item["query_normalized"] == query)
    assert gap["status"] == "open" and gap["replay_status"] is None

    # 2. 上传官方资料并关联到这条缺口
    body = upload(client, token, content=(SYNTHETIC_DIR / "RC-S200_manual.pdf").read_bytes())
    linked = client.patch(
        "/api/v1/admin/content-gaps",
        headers=auth(token),
        json={
            "robot_model_id": robot_model_id,
            "query_normalized": query,
            "linked_document_id": body["document_id"],
            "status": "investigating",
            "note": "已上传官方手册",
        },
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["linked_document_id"] == body["document_id"]
    assert linked.json()["status"] == "investigating"

    # 3. 重放原问题：能引用作答即判定缺口已补上
    client.app.state.generation_provider = ScriptedGenerationProvider(
        ["尘盒容量为 400 毫升，水箱容量为 240 毫升。[1]"]
    )
    replay = client.post(
        "/api/v1/admin/content-gaps/replay",
        headers=auth(token),
        json={"robot_model_id": robot_model_id, "query_normalized": query},
    )
    assert replay.status_code == 200, replay.text
    outcome = replay.json()
    assert outcome["replay_status"] == "passed"
    assert outcome["citation_count"] >= 1
    assert outcome["gap_status"] == "resolved"
    assert outcome["checked_at"] is not None

    # 4. 缺口榜上直接看到已解决 + 复测证据
    refreshed = client.get("/api/v1/admin/content-gaps", headers=auth(token)).json()
    closed = next(item for item in refreshed if item["query_normalized"] == query)
    assert closed["status"] == "resolved"
    assert closed["replay_status"] == "passed"
    assert closed["linked_document_title"]
    assert closed["resolved_at"] is not None

    with client.app.state.session_factory() as db:
        actions = {
            audit.action
            for audit in db.scalars(
                select(AuditLog).where(AuditLog.action.like("admin.content_gap.%"))
            )
        }
    assert actions == {"admin.content_gap.update", "admin.content_gap.replay"}


def test_replay_failure_downgrades_a_previously_resolved_gap(client):
    """复测不通过必须把"已解决"打回去，否则缺口榜开始骗人。"""

    token = setup_admin(client)
    robot_model_id = model_id_by_code(client, "RC-S200")
    query = "完全 没有 资料 的问题"
    insert_gap_event(client, model_code="RC-S200", query=query)
    client.patch(
        "/api/v1/admin/content-gaps",
        headers=auth(token),
        json={
            "robot_model_id": robot_model_id,
            "query_normalized": query,
            "status": "resolved",
        },
    )

    replay = client.post(
        "/api/v1/admin/content-gaps/replay",
        headers=auth(token),
        json={"robot_model_id": robot_model_id, "query_normalized": query},
    )
    assert replay.status_code == 200, replay.text
    body = replay.json()
    assert body["replay_status"] == "failed"
    assert body["gap_status"] == "investigating"

    with client.app.state.session_factory() as db:
        resolution = db.scalar(
            select(ContentGapResolution).where(
                ContentGapResolution.robot_model_id == robot_model_id
            )
        )
    assert resolution.resolved_at is None


def test_replay_marks_error_when_generation_backend_is_down(client):
    """外部模型挂了属于"没测成"，不能算"没解决"——状态不该被误改。"""

    token = setup_admin(client)
    robot_model_id = model_id_by_code(client, "RC-S200")
    # 查询必须真能检索命中，否则会先撞 knowledge_gap 拒答，根本走不到模型调用，
    # 测出来的 failed 是假的。
    query = "尘盒容量 400 毫升，水箱容量 240 毫升"
    insert_gap_event(client, model_code="RC-S200", query=query)
    upload(client, token, content=(SYNTHETIC_DIR / "RC-S200_manual.pdf").read_bytes())

    class BrokenProvider:
        model_name = "broken-test-model"

        def generate(self, *, system: str, prompt: str):
            raise RuntimeError("upstream unavailable")

    client.app.state.generation_provider = BrokenProvider()
    replay = client.post(
        "/api/v1/admin/content-gaps/replay",
        headers=auth(token),
        json={"robot_model_id": robot_model_id, "query_normalized": query},
    )
    assert replay.status_code == 200, replay.text
    body = replay.json()
    assert body["replay_status"] == "error"
    assert body["gap_status"] == "open", "复测未完成不应改变缺口状态"


def test_gap_endpoints_reject_unknown_gap_and_cross_model_document(client):
    token = setup_admin(client)
    robot_model_id = model_id_by_code(client, "RC-S200")
    body = upload(client, token, content=(SYNTHETIC_DIR / "RC-S200_manual.pdf").read_bytes())

    unknown = client.patch(
        "/api/v1/admin/content-gaps",
        headers=auth(token),
        json={"robot_model_id": robot_model_id, "query_normalized": "从未出现过的问题"},
    )
    assert unknown.status_code == 404

    insert_gap_event(client, model_code="JH69U1", query="跨型号 关联 测试")
    other_model_id = model_id_by_code(client, "JH69U1")
    cross = client.patch(
        "/api/v1/admin/content-gaps",
        headers=auth(token),
        json={
            "robot_model_id": other_model_id,
            "query_normalized": "跨型号 关联 测试",
            "linked_document_id": body["document_id"],
        },
    )
    assert cross.status_code == 422, "缺口只能关联同型号的文档，否则复测查的不是同一个库"


def test_knowledge_admin_endpoints_require_admin_role(client):
    token = setup_admin(client)
    body = upload(client, token, content=(SYNTHETIC_DIR / "RC-S200_manual.pdf").read_bytes())
    user_token = register(client, "plain-user@example.com")["access_token"]
    document_id = body["document_id"]

    for method, url, payload in [
        ("get", "/api/v1/admin/knowledge/documents", None),
        ("get", f"/api/v1/admin/knowledge/documents/{document_id}", None),
        ("get", f"/api/v1/admin/knowledge/documents/{document_id}/chunks", None),
        ("get", f"/api/v1/admin/knowledge/documents/{document_id}/file", None),
        ("patch", f"/api/v1/admin/knowledge/documents/{document_id}", {"status": "disabled"}),
        ("delete", f"/api/v1/admin/knowledge/documents/{document_id}", None),
        ("post", f"/api/v1/admin/knowledge/documents/{document_id}/reindex", None),
        ("post", f"/api/v1/admin/knowledge/documents/{document_id}/rollback", {"version": 1}),
        (
            "post",
            "/api/v1/admin/content-gaps/replay",
            {"robot_model_id": 1, "query_normalized": "x"},
        ),
    ]:
        call = getattr(client, method)
        response = call(url, headers=auth(user_token), **({"json": payload} if payload else {}))
        assert response.status_code == 403, f"{method} {url} 不应对普通用户开放"
