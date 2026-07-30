"""阶段3 运营闭环测试：缺口埋点两处、缺口榜聚合、overview 运营字段、管理员知识上传。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.knowledge_service import HashingNgramEmbeddingProvider
from app.models import (
    AuditLog,
    GenerationRecord,
    KnowledgeDocument,
    KnowledgeGapEvent,
    RobotModel,
)
from app.rate_limit_service import normalize_knowledge_query
from conftest import auth, register
from test_admin_api import make_admin

SYNTHETIC_DIR = PROJECT_ROOT / "knowledge" / "synthetic"


class ScriptedGenerationProvider:
    model_name = "scripted-test-model"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def generate(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        return self.outputs.pop(0)


def model_id_by_code(client, code: str) -> int:
    with client.app.state.session_factory() as db:
        return db.scalar(select(RobotModel.id).where(RobotModel.code == code))


def gap_events(client) -> list[KnowledgeGapEvent]:
    with client.app.state.session_factory() as db:
        return list(
            db.scalars(select(KnowledgeGapEvent).order_by(KnowledgeGapEvent.id))
        )


def insert_gap_event(client, *, model_code: str, query: str, days_ago: int = 0) -> None:
    with client.app.state.session_factory() as db:
        db.add(
            KnowledgeGapEvent(
                robot_model_id=db.scalar(
                    select(RobotModel.id).where(RobotModel.code == model_code)
                ),
                query_normalized=query,
                source="search_empty",
                created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
            )
        )
        db.commit()


def test_search_empty_writes_gap_event_including_cache_hit(client):
    client.app.state.embedding_provider = HashingNgramEmbeddingProvider()
    token = register(client, "gap-search@example.com")["access_token"]
    robot_model_id = model_id_by_code(client, "JH69U1")

    raw_query = "  有没有 自动集尘 的  设置 "
    for _ in range(2):  # 第二次命中缓存，同样必须记缺口
        response = client.post(
            "/api/v1/knowledge/search",
            headers=auth(token),
            json={"robot_model_id": robot_model_id, "query": raw_query},
        )
        assert response.status_code == 200, response.text
        assert response.json() == []

    events = gap_events(client)
    assert len(events) == 2
    for event in events:
        assert event.source == "search_empty"
        assert event.robot_model_id == robot_model_id
        assert event.query_normalized == normalize_knowledge_query(raw_query)
        assert event.created_at is not None


def test_gap_event_write_failure_does_not_break_search(client, monkeypatch):
    client.app.state.embedding_provider = HashingNgramEmbeddingProvider()
    token = register(client, "gap-failure@example.com")["access_token"]
    robot_model_id = model_id_by_code(client, "JH69U1")

    original_commit = Session.commit

    def reject_gap_commit(self: Session) -> None:
        if any(isinstance(item, KnowledgeGapEvent) for item in self.new):
            raise RuntimeError("gap event database unavailable")
        original_commit(self)

    monkeypatch.setattr(Session, "commit", reject_gap_commit)
    response = client.post(
        "/api/v1/knowledge/search",
        headers=auth(token),
        json={"robot_model_id": robot_model_id, "query": "找不到的问题"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == []
    assert gap_events(client) == []


def test_answer_knowledge_gap_refusal_writes_gap_event(client):
    client.app.state.embedding_provider = HashingNgramEmbeddingProvider()
    client.app.state.generation_provider = ScriptedGenerationProvider([])
    token = register(client, "gap-answer@example.com")["access_token"]
    robot_model_id = model_id_by_code(client, "RC-X800")  # 未入库任何知识

    response = client.post(
        "/api/v1/knowledge/answer",
        headers=auth(token),
        json={"robot_model_id": robot_model_id, "query": "支持 自动更换拖布 吗"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "refused"
    assert response.json()["refusal_reason"] == "knowledge_gap"

    events = gap_events(client)
    assert len(events) == 1
    assert events[0].source == "answer_knowledge_gap"
    assert events[0].robot_model_id == robot_model_id
    assert events[0].query_normalized == normalize_knowledge_query("支持 自动更换拖布 吗")


def test_content_gaps_aggregates_orders_and_filters_by_days(client):
    admin_token = make_admin(client)
    # 热门缺口：两个型号各一次 + JH69U1 再一次 = 3 次
    insert_gap_event(client, model_code="JH69U1", query="石头卡住 怎么办", days_ago=2)
    insert_gap_event(client, model_code="VC35U1", query="石头卡住 怎么办", days_ago=1)
    insert_gap_event(client, model_code="JH69U1", query="石头卡住 怎么办", days_ago=0)
    # 次热缺口：1 次
    insert_gap_event(client, model_code="JH69U1", query="滤网 多久换一次", days_ago=3)
    # 过期缺口：40 天前，默认 30 天窗口内不可见
    insert_gap_event(client, model_code="JH69U1", query="很久以前的问题", days_ago=40)

    response = client.get("/api/v1/admin/content-gaps", headers=auth(admin_token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["query_normalized"] for item in body] == [
        "石头卡住 怎么办",
        "滤网 多久换一次",
    ]
    top = body[0]
    assert top["count"] == 3
    assert top["model_codes"] == ["JH69U1", "VC35U1"]
    assert top["last_seen_at"] is not None
    assert body[1]["count"] == 1 and body[1]["model_codes"] == ["JH69U1"]
    # 响应不含任何用户信息
    for item in body:
        assert set(item) == {"query_normalized", "count", "model_codes", "last_seen_at"}

    # 放宽天数窗口后能看到旧缺口；limit 生效
    wide = client.get(
        "/api/v1/admin/content-gaps?days=60&limit=1", headers=auth(admin_token)
    )
    assert wide.status_code == 200
    assert len(wide.json()) == 1
    full = client.get("/api/v1/admin/content-gaps?days=60", headers=auth(admin_token))
    assert {item["query_normalized"] for item in full.json()} >= {"很久以前的问题"}

    # 参数校验
    assert client.get(
        "/api/v1/admin/content-gaps?days=0", headers=auth(admin_token)
    ).status_code == 422
    assert client.get(
        "/api/v1/admin/content-gaps?limit=101", headers=auth(admin_token)
    ).status_code == 422


def test_overview_reports_generation_stats_and_content_gap_count(client):
    admin_token = make_admin(client)
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as db:
        robot_model_id = db.scalar(select(RobotModel.id).where(RobotModel.code == "JH69U1"))

        def record(status: str, reason: str | None, days_ago: int) -> GenerationRecord:
            return GenerationRecord(
                robot_model_id=robot_model_id,
                query="统计用查询",
                prompt_version="answer-v1",
                provider_model="scripted-test-model",
                status=status,
                answer="回答 [1]" if status == "answered" else None,
                citations_json=[],
                refusal_reason=reason,
                snippets_sha256="0" * 64,
                snippet_count=0,
                latency_ms=1.0,
                created_at=now - timedelta(days=days_ago),
            )

        db.add_all(
            [
                record("answered", None, 1),
                record("answered", None, 2),
                record("refused", "knowledge_gap", 1),
                record("refused", "knowledge_gap", 3),
                record("refused", "citation_invalid", 2),
                record("answered", None, 45),  # 30 天窗口外，不计入
                record("refused", "model_refused", 45),  # 30 天窗口外，不计入
            ]
        )
        db.commit()
    insert_gap_event(client, model_code="JH69U1", query="缺口一", days_ago=1)
    insert_gap_event(client, model_code="VC35U1", query="缺口二", days_ago=2)
    insert_gap_event(client, model_code="JH69U1", query="旧缺口", days_ago=40)

    response = client.get("/api/v1/admin/overview", headers=auth(admin_token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["generation_stats"] == {
        "answered_count": 2,
        "refused_count": 3,
        "refusal_by_reason": {"knowledge_gap": 2, "citation_invalid": 1},
    }
    assert body["content_gap_count"] == 2


def test_admin_knowledge_upload_ingests_pdf_and_writes_audit(client):
    admin_token = make_admin(client)
    client.app.state.embedding_provider = HashingNgramEmbeddingProvider()
    pdf_bytes = (SYNTHETIC_DIR / "RC-S200_manual.pdf").read_bytes()

    response = client.post(
        "/api/v1/admin/knowledge/upload",
        headers=auth(admin_token),
        data={"model_code": "RC-S200", "source_url": "synthetic://upload/rc-s200/manual"},
        files={"file": ("RC-S200_manual.pdf", pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True
    assert body["chunk_count"] > 0
    assert len(body["sha256"]) == 64

    with client.app.state.session_factory() as db:
        document = db.get(KnowledgeDocument, body["document_id"])
        assert document is not None
        assert document.sha256 == body["sha256"]
        audits = list(
            db.scalars(select(AuditLog).where(AuditLog.action == "admin.knowledge.upload"))
        )
    assert len(audits) == 1
    assert audits[0].resource_id == str(body["document_id"])
    assert audits[0].details_json["model_code"] == "RC-S200"
    assert audits[0].details_json["sha256"] == body["sha256"]
    assert audits[0].details_json["chunk_count"] == body["chunk_count"]
    # 审计不含文件内容
    assert "content" not in audits[0].details_json

    status = client.get("/api/v1/admin/knowledge/status", headers=auth(admin_token))
    by_code = {item["model_code"]: item for item in status.json()}
    assert by_code["RC-S200"]["document_count"] == 1
    assert by_code["RC-S200"]["chunk_count"] == body["chunk_count"]


def test_admin_knowledge_upload_rejects_non_pdf_without_partial_records(client):
    admin_token = make_admin(client)
    client.app.state.embedding_provider = HashingNgramEmbeddingProvider()

    response = client.post(
        "/api/v1/admin/knowledge/upload",
        headers=auth(admin_token),
        data={"model_code": "RC-S200", "source_url": "synthetic://upload/fake"},
        files={"file": ("fake.pdf", b"this is not a pdf", "application/pdf")},
    )
    assert response.status_code == 415

    missing_model = client.post(
        "/api/v1/admin/knowledge/upload",
        headers=auth(admin_token),
        data={"model_code": "NO-SUCH-MODEL", "source_url": "synthetic://upload/none"},
        files={"file": ("manual.pdf", b"%PDF-1.4 minimal", "application/pdf")},
    )
    assert missing_model.status_code == 404

    with client.app.state.session_factory() as db:
        assert list(db.scalars(select(KnowledgeDocument))) == []
        assert list(
            db.scalars(select(AuditLog).where(AuditLog.action == "admin.knowledge.upload"))
        ) == []


def test_admin_knowledge_upload_rejects_normal_users(client):
    token = register(client, "normal-upload@example.com")["access_token"]
    response = client.post(
        "/api/v1/admin/knowledge/upload",
        headers=auth(token),
        data={"model_code": "RC-S200", "source_url": "synthetic://upload/forbidden"},
        files={"file": ("manual.pdf", b"%PDF-1.4 minimal", "application/pdf")},
    )
    assert response.status_code == 403
    with client.app.state.session_factory() as db:
        assert list(db.scalars(select(KnowledgeDocument))) == []
