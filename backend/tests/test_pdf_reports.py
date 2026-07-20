import re

from conftest import auth, image_bytes, register, submit_feedback


def create_diagnostic(client, token: str) -> int:
    model_id = next(item["id"] for item in client.get("/api/v1/models").json() if item["code"] == "VC35U1")
    device = client.post(
        "/api/v1/devices",
        headers=auth(token),
        json={"robot_model_id": model_id, "nickname": "客厅扫地机器人"},
    )
    assert device.status_code == 201, device.text
    diagnostic = client.post(
        "/api/v1/diagnostics",
        headers=auth(token),
        json={
            "device_id": device.json()["id"],
            "issue_category_code": "wifi_setup_failure",
            "issue_description": "配网多次失败，指示灯持续闪烁。",
            "error_code": "WIFI-01",
        },
    )
    assert diagnostic.status_code == 201, diagnostic.text
    return diagnostic.json()["id"]


def finish(client, token: str, diagnostic_id: int, final_outcome: str) -> None:
    for position in range(1, 4):
        outcome = final_outcome if position == 3 else "not_resolved"
        response = submit_feedback(client, token, diagnostic_id, outcome)
        assert response.status_code == 200, response.text


def test_unresolved_pdf_report_is_valid_nonempty_and_idempotent(client):
    token = register(client, "pdf-owner@example.com")["access_token"]
    diagnostic_id = create_diagnostic(client, token)
    uploaded = client.post(
        f"/api/v1/diagnostics/{diagnostic_id}/attachments",
        headers=auth(token),
        files={"file": ("故障截图.png", image_bytes("PNG"), "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    finish(client, token, diagnostic_id, "not_resolved")

    endpoint = f"/api/v1/diagnostics/{diagnostic_id}/report/pdf"
    created = client.post(endpoint, headers=auth(token))
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["size_bytes"] > 500
    assert payload["download_url"] == endpoint
    assert payload["filename"].endswith(".pdf")

    stored_files = list(client.app.state.report_dir.iterdir())
    assert len(stored_files) == 1
    assert re.fullmatch(r"[0-9a-f]{64}\.pdf", stored_files[0].name)
    assert payload["report_number"] not in stored_files[0].name

    repeated = client.post(endpoint, headers=auth(token))
    assert repeated.status_code == 201
    assert repeated.json() == payload
    assert [item.name for item in client.app.state.report_dir.iterdir()] == [stored_files[0].name]

    downloaded = client.get(endpoint, headers=auth(token))
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/pdf"
    assert downloaded.content.startswith(b"%PDF-")
    assert len(downloaded.content) == payload["size_bytes"]

    text_report = client.get(
        f"/api/v1/diagnostics/{diagnostic_id}/report", headers=auth(token)
    )
    assert text_report.status_code == 200
    assert "故障截图.png" in text_report.json()["content"]
    assert "来源：" in text_report.json()["content"]


def test_resolved_diagnostic_cannot_generate_pdf_report(client):
    token = register(client, "pdf-resolved@example.com")["access_token"]
    diagnostic_id = create_diagnostic(client, token)
    finish(client, token, diagnostic_id, "resolved")

    endpoint = f"/api/v1/diagnostics/{diagnostic_id}/report/pdf"
    assert client.post(endpoint, headers=auth(token)).status_code == 409
    assert client.get(endpoint, headers=auth(token)).status_code == 404
    assert list(client.app.state.report_dir.iterdir()) == []


def test_pdf_report_enforces_diagnostic_ownership(client):
    owner = register(client, "pdf-access-owner@example.com")["access_token"]
    stranger = register(client, "pdf-access-stranger@example.com")["access_token"]
    diagnostic_id = create_diagnostic(client, owner)
    finish(client, owner, diagnostic_id, "not_resolved")
    endpoint = f"/api/v1/diagnostics/{diagnostic_id}/report/pdf"

    assert client.post(endpoint, headers=auth(stranger)).status_code == 403
    assert client.post(endpoint, headers=auth(owner)).status_code == 201
    assert client.get(endpoint, headers=auth(stranger)).status_code == 403
    assert client.get(endpoint, headers=auth(owner)).status_code == 200
