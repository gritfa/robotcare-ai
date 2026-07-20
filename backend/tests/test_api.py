from conftest import auth, register, submit_feedback


def setup_device(client, token: str, model_code: str) -> int:
    models = client.get("/api/v1/models").json()
    model_id = next(item["id"] for item in models if item["code"] == model_code)
    response = client.post(
        "/api/v1/devices",
        headers=auth(token),
        json={"robot_model_id": model_id, "nickname": "客厅机器人"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_diagnostic(client, token: str, device_id: int, category: str) -> int:
    response = client.post(
        "/api/v1/diagnostics",
        headers=auth(token),
        json={
            "device_id": device_id,
            "issue_category_code": category,
            "issue_description": "按照正常方式操作后仍然无法使用",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_register_login_and_me(client):
    created = register(client, "user@example.com")
    assert created["token_type"] == "bearer"
    assert created["user"]["email"] == "user@example.com"

    login = client.post(
        "/api/v1/auth/login", json={"email": "USER@example.com", "password": "StrongPass123"}
    )
    assert login.status_code == 200
    me = client.get("/api/v1/auth/me", headers=auth(login.json()["access_token"]))
    assert me.status_code == 200
    assert me.json()["email"] == "user@example.com"
    assert client.post(
        "/api/v1/auth/login", json={"email": "user@example.com", "password": "wrong"}
    ).status_code == 401


def test_device_ownership_isolation(client):
    first = register(client, "first@example.com")["access_token"]
    second = register(client, "second@example.com")["access_token"]
    device_id = setup_device(client, first, "JH69U1")

    assert client.get(f"/api/v1/devices/{device_id}", headers=auth(second)).status_code == 403
    assert client.patch(
        f"/api/v1/devices/{device_id}", headers=auth(second), json={"nickname": "越权修改"}
    ).status_code == 403
    assert client.delete(f"/api/v1/devices/{device_id}", headers=auth(second)).status_code == 403
    assert client.get("/api/v1/devices", headers=auth(second)).json() == []


def test_diagnostic_step_progression_and_resolved_end(client):
    token = register(client, "resolved@example.com")["access_token"]
    device_id = setup_device(client, token, "JH69U1")
    diagnostic_id = create_diagnostic(client, token, device_id, "return_to_dock_failure")

    first_step = client.get(
        f"/api/v1/diagnostics/{diagnostic_id}/steps/current", headers=auth(token)
    ).json()
    assert first_step["position"] == 1

    moved = submit_feedback(client, token, diagnostic_id, "not_resolved")
    assert moved.status_code == 200
    assert moved.json()["current_step"]["position"] == 2

    finished = submit_feedback(client, token, diagnostic_id, "resolved")
    assert finished.status_code == 200
    assert finished.json()["diagnostic"]["status"] == "resolved"
    assert finished.json()["diagnostic"]["resolved"] is True
    assert finished.json()["current_step"] is None
    assert client.get(
        f"/api/v1/diagnostics/{diagnostic_id}/steps/current", headers=auth(token)
    ).status_code == 409
    assert client.post(
        f"/api/v1/diagnostics/{diagnostic_id}/report", headers=auth(token)
    ).status_code == 409


def test_unresolved_flow_creates_idempotent_report(client):
    token = register(client, "report@example.com")["access_token"]
    device_id = setup_device(client, token, "VC35U1")
    diagnostic_id = create_diagnostic(client, token, device_id, "wifi_setup_failure")

    for expected_next in (2, 3, None):
        response = submit_feedback(client, token, diagnostic_id, "not_resolved")
        assert response.status_code == 200, response.text
        current = response.json()["current_step"]
        assert (current["position"] if current else None) == expected_next

    detail = client.get(f"/api/v1/diagnostics/{diagnostic_id}", headers=auth(token)).json()
    assert detail["status"] == "unresolved"
    assert detail["resolved"] is False
    assert detail["report_available"] is False
    assert len(detail["executions"]) == 3

    created = client.post(f"/api/v1/diagnostics/{diagnostic_id}/report", headers=auth(token))
    assert created.status_code == 201
    assert "VC35U1" in created.json()["content"]
    assert "第三方" in created.json()["content"]
    repeated = client.post(f"/api/v1/diagnostics/{diagnostic_id}/report", headers=auth(token))
    assert repeated.status_code == 201
    assert repeated.json()["id"] == created.json()["id"]
    fetched = client.get(f"/api/v1/diagnostics/{diagnostic_id}/report", headers=auth(token))
    assert fetched.status_code == 200
    refreshed = client.get(f"/api/v1/diagnostics/{diagnostic_id}", headers=auth(token)).json()
    assert refreshed["report_available"] is True
    history_item = next(
        item for item in client.get("/api/v1/diagnostics", headers=auth(token)).json()
        if item["id"] == diagnostic_id
    )
    assert history_item["report_available"] is True


def test_diagnostic_ownership_and_model_flow_validation(client):
    owner = register(client, "owner@example.com")["access_token"]
    stranger = register(client, "stranger@example.com")["access_token"]
    device_id = setup_device(client, owner, "JH69U1")
    diagnostic_id = create_diagnostic(client, owner, device_id, "return_to_dock_failure")

    assert client.get(f"/api/v1/diagnostics/{diagnostic_id}", headers=auth(stranger)).status_code == 403
    assert client.post(
        f"/api/v1/diagnostics/{diagnostic_id}/feedback",
        headers=auth(stranger),
        json={
            "step_id": client.get(
                f"/api/v1/diagnostics/{diagnostic_id}/steps/current", headers=auth(owner)
            ).json()["id"],
            "outcome": "resolved",
        },
    ).status_code == 403
    wrong_flow = client.post(
        "/api/v1/diagnostics",
        headers=auth(owner),
        json={
            "device_id": device_id,
            "issue_category_code": "wifi_setup_failure",
            "issue_description": "这个型号没有该种子流程",
        },
    )
    assert wrong_flow.status_code == 404
