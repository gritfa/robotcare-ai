from sqlalchemy import func, select

from app.models import DiagnosticSession
from conftest import auth, register, submit_feedback


def _device(client, token: str, model_code: str) -> int:
    model_id = next(
        item["id"]
        for item in client.get("/api/v1/models").json()
        if item["code"] == model_code
    )
    response = client.post(
        "/api/v1/devices",
        headers=auth(token),
        json={"robot_model_id": model_id, "nickname": f"{model_code} test"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create(
    client,
    token: str,
    device_id: int,
    selected: str,
    description: str,
    *,
    error_code: str | None = None,
    confirm: bool = False,
):
    return client.post(
        "/api/v1/diagnostics",
        headers=auth(token),
        json={
            "device_id": device_id,
            "issue_category_code": selected,
            "issue_description": description,
            "error_code": error_code,
            "confirm_category_mismatch": confirm,
        },
    )


def test_matching_category_is_created_and_decision_is_recorded(client):
    token = register(client, "classifier-match@example.com")["access_token"]
    device_id = _device(client, token, "VC35U1")

    response = _create(
        client,
        token,
        device_id,
        "wifi_setup_failure",
        "机器人配网失败，始终无法联网",
    )

    assert response.status_code == 201, response.text
    decision = response.json()["category_decision"]
    assert decision["selected_category_code"] == "wifi_setup_failure"
    assert decision["candidate_category_codes"] == ["wifi_setup_failure"]
    assert decision["final_category_code"] == "wifi_setup_failure"
    assert decision["decision"] == "consistent"


def test_clear_category_conflict_returns_structured_error_without_session(client):
    token = register(client, "classifier-conflict@example.com")["access_token"]
    device_id = _device(client, token, "VC35U1")

    response = _create(
        client,
        token,
        device_id,
        "cleaning_noise",
        "Wi-Fi 配网失败，手机一直无法联网",
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "ISSUE_CATEGORY_MISMATCH",
        "selected": "cleaning_noise",
        "suggested": "wifi_setup_failure",
        "suggested_categories": ["wifi_setup_failure"],
        "requires_confirmation": False,
    }
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(DiagnosticSession)) == 0

    cannot_override = _create(
        client,
        token,
        device_id,
        "cleaning_noise",
        "Wi-Fi 配网失败，手机一直无法联网",
        confirm=True,
    )
    assert cannot_override.status_code == 409


def test_ambiguous_signals_require_then_accept_explicit_confirmation(client):
    token = register(client, "classifier-ambiguous@example.com")["access_token"]
    device_id = _device(client, token, "VC35U1")
    description = "机器人配网失败，同时清扫运行时有异响"

    mismatch = _create(
        client, token, device_id, "cleaning_noise", description
    )
    assert mismatch.status_code == 409
    detail = mismatch.json()["detail"]
    assert detail["code"] == "ISSUE_CATEGORY_MISMATCH"
    assert detail["requires_confirmation"] is True
    assert set(detail["suggested_categories"]) == {
        "wifi_setup_failure",
        "cleaning_noise",
    }

    confirmed = _create(
        client,
        token,
        device_id,
        "cleaning_noise",
        description,
        confirm=True,
    )
    assert confirmed.status_code == 201, confirmed.text
    decision = confirmed.json()["category_decision"]
    assert decision["final_category_code"] == "cleaning_noise"
    assert decision["confirmation"] == "keep_selected"


def test_error_code_has_priority_over_description_keywords(client):
    token = register(client, "classifier-code@example.com")["access_token"]
    device_id = _device(client, token, "VC35U1")

    response = _create(
        client,
        token,
        device_id,
        "cleaning_noise",
        "清扫运行时有明显异响",
        error_code="WIFI-01",
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["suggested"] == "wifi_setup_failure"
    assert detail["suggested_categories"] == ["wifi_setup_failure"]


def test_category_candidates_are_isolated_by_robot_model(client):
    token = register(client, "classifier-model@example.com")["access_token"]
    device_id = _device(client, token, "JH69U1")

    response = _create(
        client,
        token,
        device_id,
        "return_to_dock_failure",
        "Wi-Fi 配网失败并且无法联网",
    )

    assert response.status_code == 201, response.text
    assert response.json()["category_decision"]["candidate_category_codes"] == []


def test_safety_block_still_precedes_category_conflict(client):
    token = register(client, "classifier-safety@example.com")["access_token"]
    device_id = _device(client, token, "VC35U1")

    response = _create(
        client,
        token,
        device_id,
        "cleaning_noise",
        "Wi-Fi 配网失败，而且机器人正在冒烟",
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SAFETY_BLOCKED"


def test_service_report_records_selected_candidates_and_final_category(client):
    token = register(client, "classifier-report@example.com")["access_token"]
    device_id = _device(client, token, "VC35U1")
    created = _create(
        client,
        token,
        device_id,
        "wifi_setup_failure",
        "Wi-Fi 配网失败，手机无法联网",
        error_code="WIFI-01",
    )
    assert created.status_code == 201, created.text
    diagnostic_id = created.json()["id"]
    for _ in range(3):
        feedback = submit_feedback(
            client, token, diagnostic_id, "not_resolved"
        )
        assert feedback.status_code == 200, feedback.text

    report = client.post(
        f"/api/v1/diagnostics/{diagnostic_id}/report", headers=auth(token)
    )
    assert report.status_code == 201, report.text
    content = report.json()["content"]
    assert "用户选择类别：wifi_setup_failure" in content
    assert "规则候选类别：wifi_setup_failure" in content
    assert "最终类别：wifi_setup_failure" in content
