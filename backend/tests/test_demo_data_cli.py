from pathlib import Path

from app.demo_data_cli import (
    DEFAULT_DEMO_PASSWORD,
    demo_summary,
    load_demo_data,
    reset_demo_data,
)


def login(client, email: str):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": DEFAULT_DEMO_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_demo_dataset_covers_user_admin_and_lifecycle_states(client, tmp_path):
    attachment_dir = tmp_path / "attachments"
    report_dir = tmp_path / "reports"
    with client.app.state.session_factory() as db:
        result = load_demo_data(
            db,
            attachment_dir=attachment_dir,
            report_dir=report_dir,
            report_secret=client.app.state.report_filename_secret,
        )
        summary = demo_summary(db)

    assert result["synthetic"] is True
    assert summary == {
        "synthetic": True,
        "users": 7,
        "devices": 10,
        "diagnostics": 12,
        "diagnostic_statuses": {
            "in_progress": 4,
            "resolved": 4,
            "unresolved": 4,
        },
        "attachments": 4,
        "reports": 4,
        "safety_blocks": 4,
    }
    assert len(list(attachment_dir.glob("demo-diagnostic-*.png"))) == 4
    assert len(list(report_dir.glob("*.pdf"))) == 4

    user_token = login(client, "demo.alice@example.com")
    devices = client.get("/api/v1/devices", headers=auth(user_token))
    diagnostics = client.get("/api/v1/diagnostics", headers=auth(user_token))
    assert devices.status_code == 200 and len(devices.json()) == 2
    assert diagnostics.status_code == 200 and diagnostics.json()
    assert {item["status"] for item in diagnostics.json()} <= {
        "in_progress",
        "resolved",
        "unresolved",
    }

    admin_token = login(client, "demo.admin@example.com")
    overview = client.get("/api/v1/admin/overview", headers=auth(admin_token))
    safety_blocks = client.get("/api/v1/admin/safety-blocks", headers=auth(admin_token))
    reports = client.get("/api/v1/admin/unresolved-reports", headers=auth(admin_token))
    assert overview.status_code == 200
    assert overview.json()["user_count"] == 7
    assert safety_blocks.status_code == 200 and len(safety_blocks.json()) == 4
    assert reports.status_code == 200 and len(reports.json()) == 4


def test_demo_reset_removes_only_managed_rows_and_files(client, tmp_path):
    attachment_dir = Path(tmp_path / "attachments")
    report_dir = Path(tmp_path / "reports")
    kept_user = client.post(
        "/api/v1/auth/register",
        json={"email": "kept.user@example.com", "password": "KeepMe123"},
    )
    assert kept_user.status_code == 201
    with client.app.state.session_factory() as db:
        load_demo_data(
            db,
            attachment_dir=attachment_dir,
            report_dir=report_dir,
            report_secret=client.app.state.report_filename_secret,
        )

    # Create authentication rows too, so reset proves the complete user lifecycle is removable.
    login(client, "demo.alice@example.com")

    with client.app.state.session_factory() as db:
        removed = reset_demo_data(
            db,
            attachment_dir=attachment_dir,
            report_dir=report_dir,
            report_secret=client.app.state.report_filename_secret,
        )
        assert demo_summary(db) == {"synthetic": True, "users": 0, "diagnostics": 0}

    assert removed == {"users": 7, "diagnostics": 12, "attachments": 4, "reports": 4}
    assert list(attachment_dir.glob("demo-diagnostic-*.png")) == []
    assert list(report_dir.glob("*.pdf")) == []
    failed_login = client.post(
        "/api/v1/auth/login",
        json={"email": "demo.alice@example.com", "password": DEFAULT_DEMO_PASSWORD},
    )
    assert failed_login.status_code == 401
    kept_login = client.post(
        "/api/v1/auth/login",
        json={"email": "kept.user@example.com", "password": "KeepMe123"},
    )
    assert kept_login.status_code == 200
