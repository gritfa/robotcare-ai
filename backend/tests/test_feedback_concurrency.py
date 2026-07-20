from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select

from app.models import DiagnosticSession, StepExecution
from conftest import auth, register


def create_session(client, token: str) -> tuple[int, int]:
    model_id = next(
        item["id"] for item in client.get("/api/v1/models").json() if item["code"] == "JH69U1"
    )
    device = client.post(
        "/api/v1/devices",
        headers=auth(token),
        json={"robot_model_id": model_id, "nickname": "反馈并发测试设备"},
    )
    diagnostic = client.post(
        "/api/v1/diagnostics",
        headers=auth(token),
        json={
            "device_id": device.json()["id"],
            "issue_category_code": "return_to_dock_failure",
            "issue_description": "机器人无法正常返回充电座",
        },
    )
    diagnostic_id = diagnostic.json()["id"]
    step = client.get(
        f"/api/v1/diagnostics/{diagnostic_id}/steps/current", headers=auth(token)
    ).json()
    return diagnostic_id, step["id"]


def post_feedback(client, token: str, diagnostic_id: int, step_id: int):
    return client.post(
        f"/api/v1/diagnostics/{diagnostic_id}/feedback",
        headers=auth(token),
        json={"step_id": step_id, "outcome": "not_resolved"},
    )


def test_duplicate_and_stale_feedback_return_conflict_without_duplicate_execution(client):
    token = register(client, "feedback-repeat@example.com")["access_token"]
    diagnostic_id, first_step_id = create_session(client, token)

    first = post_feedback(client, token, diagnostic_id, first_step_id)
    assert first.status_code == 200
    duplicate = post_feedback(client, token, diagnostic_id, first_step_id)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "STALE_DIAGNOSTIC_STEP"

    second_step_id = first.json()["current_step"]["id"]
    stale = post_feedback(client, token, diagnostic_id, first_step_id)
    assert stale.status_code == 409
    assert second_step_id != first_step_id

    with client.app.state.session_factory() as db:
        executions = db.scalar(
            select(func.count()).select_from(StepExecution).where(
                StepExecution.session_id == diagnostic_id,
                StepExecution.step_id == first_step_id,
            )
        )
        assert executions == 1


def test_concurrent_feedback_has_one_success_and_one_conflict(client):
    token = register(client, "feedback-concurrent@example.com")["access_token"]
    diagnostic_id, step_id = create_session(client, token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda _: post_feedback(client, token, diagnostic_id, step_id),
                range(2),
            )
        )

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert all(response.status_code != 500 for response in responses)

    with client.app.state.session_factory() as db:
        executions = db.scalar(
            select(func.count()).select_from(StepExecution).where(
                StepExecution.session_id == diagnostic_id,
                StepExecution.step_id == step_id,
            )
        )
        diagnostic = db.get(DiagnosticSession, diagnostic_id)
        assert executions == 1
        assert diagnostic is not None
        assert diagnostic.current_position == 2
