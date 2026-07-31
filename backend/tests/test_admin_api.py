from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, User
from conftest import auth, register, submit_feedback


ADMIN_PATHS = (
    "/api/v1/admin/overview",
    "/api/v1/admin/models",
    "/api/v1/admin/knowledge/status",
    "/api/v1/admin/content-gaps",
    "/api/v1/admin/safety-blocks",
    "/api/v1/admin/unresolved-reports",
    "/api/v1/admin/audit-logs",
)


def make_admin(client: TestClient, email: str = "admin@example.com") -> str:
    token = register(client, email)["access_token"]
    with client.app.state.session_factory() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = "admin"
        db.commit()
    return token


def model_id(client: TestClient, code: str) -> int:
    return next(item["id"] for item in client.get("/api/v1/models").json() if item["code"] == code)


def create_device(client: TestClient, token: str, code: str) -> int:
    response = client.post(
        "/api/v1/devices",
        headers=auth(token),
        json={"robot_model_id": model_id(client, code), "nickname": "Test robot"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_diagnostic(
    client: TestClient,
    token: str,
    device_id: int,
    category: str,
    description: str = "The robot still cannot complete the expected operation",
) -> int:
    response = client.post(
        "/api/v1/diagnostics",
        headers=auth(token),
        json={
            "device_id": device_id,
            "issue_category_code": category,
            "issue_description": description,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_all_admin_endpoints_reject_normal_users(client: TestClient):
    token = register(client, "normal@example.com")["access_token"]
    for path in ADMIN_PATHS:
        response = client.get(path, headers=auth(token))
        assert response.status_code == 403, (path, response.text)

    response = client.patch(
        f"/api/v1/admin/models/{model_id(client, 'JH69U1')}",
        headers=auth(token),
        json={"active": False},
    )
    assert response.status_code == 403

    for path in (
        "/api/v1/admin/reports/1",
        "/api/v1/admin/diagnostics/1",
        "/api/v1/admin/safety-blocks/1",
    ):
        assert client.get(path, headers=auth(token)).status_code == 403


def test_admin_reads_operational_overview_without_sensitive_payloads(client: TestClient):
    admin_token = make_admin(client)
    user_token = register(client, "customer@example.com")["access_token"]

    blocked_device_id = create_device(client, user_token, "JH69U1")
    blocked = client.post(
        "/api/v1/diagnostics",
        headers=auth(user_token),
        json={
            "device_id": blocked_device_id,
            "issue_category_code": "return_to_dock_failure",
            "issue_description": "机器正在冒烟",
        },
    )
    assert blocked.status_code == 422

    report_device_id = create_device(client, user_token, "VC35U1")
    diagnostic_id = create_diagnostic(
        client,
        user_token,
        report_device_id,
        "wifi_setup_failure",
    )
    while True:
        result = submit_feedback(client, user_token, diagnostic_id, "not_resolved")
        assert result.status_code == 200, result.text
        if result.json()["current_step"] is None:
            break
    report = client.post(
        f"/api/v1/diagnostics/{diagnostic_id}/report",
        headers=auth(user_token),
    )
    assert report.status_code == 201

    overview = client.get("/api/v1/admin/overview", headers=auth(admin_token))
    assert overview.status_code == 200
    assert overview.json() == {
        "user_count": 2,
        "active_model_count": 5,  # 2 真实型号 + 3 D1 合成演示型号
        "published_flow_count": 35,  # 4 条真实 + 31 条 D1 合成演示流程
        "knowledge_document_count": 0,
        "knowledge_chunk_count": 0,
        "safety_block_count": 1,
        "unresolved_diagnostic_count": 1,
        "service_report_count": 1,
        "generation_stats": {
            "answered_count": 0,
            "refused_count": 0,
            "refusal_by_reason": {},
        },
        "content_gap_count": 0,
    }

    models = client.get("/api/v1/admin/models", headers=auth(admin_token))
    assert models.status_code == 200
    assert {item["code"] for item in models.json()} == {"JH69U1", "VC35U1", "RC-S200", "RC-M500", "RC-X800"}
    assert all("active" in item for item in models.json())

    knowledge = client.get("/api/v1/admin/knowledge/status", headers=auth(admin_token))
    assert knowledge.status_code == 200
    assert {item["model_code"] for item in knowledge.json()} == {"JH69U1", "VC35U1", "RC-S200", "RC-M500", "RC-X800"}

    safety_blocks = client.get("/api/v1/admin/safety-blocks", headers=auth(admin_token))
    assert safety_blocks.status_code == 200
    assert len(safety_blocks.json()) == 1
    assert safety_blocks.json()[0]["model_code"] == "JH69U1"
    assert "user_id" not in safety_blocks.json()[0]
    assert "device_id" not in safety_blocks.json()[0]
    assert "description_sha256" not in safety_blocks.json()[0]
    assert "reason" not in safety_blocks.json()[0]
    assert "advice" not in safety_blocks.json()[0]

    unresolved = client.get(
        "/api/v1/admin/unresolved-reports",
        headers=auth(admin_token),
    )
    assert unresolved.status_code == 200
    assert len(unresolved.json()) == 1
    entry = unresolved.json()[0]
    assert entry["report"]["id"] == report.json()["id"]
    assert "content" not in entry["report"]
    assert entry["diagnostic"]["id"] == diagnostic_id
    assert "issue_description" not in entry["diagnostic"]
    assert "error_code" not in entry["diagnostic"]
    assert entry["model"]["code"] == "VC35U1"
    assert entry["user"]["email_masked"] == "c***@example.com"
    assert "id" not in entry["user"]
    assert "email" not in entry["user"]

    safety_detail = client.get(
        f"/api/v1/admin/safety-blocks/{safety_blocks.json()[0]['id']}",
        headers=auth(admin_token),
    )
    assert safety_detail.status_code == 200
    assert safety_detail.json()["reason"]
    assert safety_detail.json()["advice"]

    diagnostic_detail = client.get(
        f"/api/v1/admin/diagnostics/{diagnostic_id}",
        headers=auth(admin_token),
    )
    assert diagnostic_detail.status_code == 200
    assert diagnostic_detail.json()["issue_description"]

    report_detail = client.get(
        f"/api/v1/admin/reports/{report.json()['id']}",
        headers=auth(admin_token),
    )
    assert report_detail.status_code == 200
    assert report_detail.json()["content"] == report.json()["content"]

    with client.app.state.session_factory() as db:
        sensitive_audits = list(
            db.scalars(
                select(AuditLog).where(AuditLog.action.like("admin.%.sensitive_read"))
            )
        )
    assert {item.action for item in sensitive_audits} == {
        "admin.service_report.sensitive_read",
        "admin.diagnostic_session.sensitive_read",
        "admin.safety_block_event.sensitive_read",
    }
    for item in sensitive_audits:
        assert item.actor_user_id is not None
        assert item.resource_id
        assert item.created_at is not None
        serialized = str(item.details_json)
        assert "trace_id" in item.details_json
        assert report_detail.json()["content"] not in serialized
        assert diagnostic_detail.json()["issue_description"] not in serialized
        assert safety_detail.json()["reason"] not in serialized
        assert safety_detail.json()["advice"] not in serialized


def test_sensitive_admin_detail_fails_closed_when_audit_cannot_commit(
    client: TestClient, monkeypatch
):
    admin_token = make_admin(client)
    user_token = register(client, "audit-failure-customer@example.com")["access_token"]
    device_id = create_device(client, user_token, "JH69U1")
    diagnostic_id = create_diagnostic(
        client,
        user_token,
        device_id,
        "return_to_dock_failure",
        description="Sensitive description must not leave without an audit record",
    )

    original_commit = Session.commit

    def reject_sensitive_audit(self: Session) -> None:
        if any(
            isinstance(item, AuditLog) and item.action.endswith(".sensitive_read")
            for item in self.new
        ):
            raise RuntimeError("audit database unavailable")
        original_commit(self)

    monkeypatch.setattr(Session, "commit", reject_sensitive_audit)
    response = client.get(
        f"/api/v1/admin/diagnostics/{diagnostic_id}",
        headers=auth(admin_token),
    )

    assert response.status_code == 503
    assert "Sensitive description" not in response.text
    with client.app.state.session_factory() as db:
        assert list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.action == "admin.diagnostic_session.sensitive_read"
                )
            )
        ) == []


def test_model_activation_changes_public_availability_and_writes_audit(client: TestClient):
    admin_token = make_admin(client)
    user_token = register(client, "device-owner@example.com")["access_token"]
    target_model_id = model_id(client, "JH69U1")
    device_id = create_device(client, user_token, "JH69U1")
    historical_id = create_diagnostic(
        client,
        user_token,
        device_id,
        "return_to_dock_failure",
    )

    changed = client.patch(
        f"/api/v1/admin/models/{target_model_id}",
        headers=auth(admin_token),
        json={"active": False},
    )
    assert changed.status_code == 200
    assert changed.json()["active"] is False
    assert "JH69U1" not in {item["code"] for item in client.get("/api/v1/models").json()}
    assert client.get(
        f"/api/v1/models/{target_model_id}/diagnostic-options",
        headers=auth(user_token),
    ).status_code == 404
    assert client.post(
        "/api/v1/devices",
        headers=auth(user_token),
        json={"robot_model_id": target_model_id, "nickname": "Disabled model"},
    ).status_code == 404
    assert client.post(
        "/api/v1/diagnostics",
        headers=auth(user_token),
        json={
            "device_id": device_id,
            "issue_category_code": "return_to_dock_failure",
            "issue_description": "A new issue after the model was disabled",
        },
    ).status_code == 409
    assert client.get(
        f"/api/v1/diagnostics/{historical_id}", headers=auth(user_token)
    ).status_code == 200

    audits = client.get("/api/v1/admin/audit-logs", headers=auth(admin_token))
    assert audits.status_code == 200
    assert len(audits.json()) == 1
    audit = audits.json()[0]
    assert audit["actor_user_id"] is not None
    assert audit["action"] == "robot_model.active_set"
    assert audit["resource_type"] == "robot_model"
    assert audit["resource_id"] == str(target_model_id)
    assert audit["details_json"] == {
        "code": "JH69U1",
        "previous_active": True,
        "active": False,
    }
    assert "password" not in str(audit).lower()
    assert "token" not in str(audit).lower()


def test_admin_list_limits_are_validated(client: TestClient):
    admin_token = make_admin(client)
    for path in (
        "/api/v1/admin/safety-blocks",
        "/api/v1/admin/unresolved-reports",
        "/api/v1/admin/audit-logs",
    ):
        assert client.get(f"{path}?limit=1", headers=auth(admin_token)).status_code == 200
        assert client.get(f"{path}?limit=0", headers=auth(admin_token)).status_code == 422
        assert client.get(f"{path}?limit=101", headers=auth(admin_token)).status_code == 422
