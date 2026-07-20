from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

from PIL import Image
from sqlalchemy import func, select

from app.models import Attachment, PendingFileDeletion
from conftest import auth, image_bytes, register, submit_feedback


MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


def create_diagnostic(client, token: str, model_code: str = "VC35U1") -> int:
    models = client.get("/api/v1/models").json()
    model_id = next(item["id"] for item in models if item["code"] == model_code)
    device = client.post(
        "/api/v1/devices",
        headers=auth(token),
        json={"robot_model_id": model_id, "nickname": "Test robot"},
    )
    assert device.status_code == 201, device.text
    category = "wifi_setup_failure" if model_code == "VC35U1" else "return_to_dock_failure"
    diagnostic = client.post(
        "/api/v1/diagnostics",
        headers=auth(token),
        json={
            "device_id": device.json()["id"],
            "issue_category_code": category,
            "issue_description": "The robot still cannot complete the requested operation.",
        },
    )
    assert diagnostic.status_code == 201, diagnostic.text
    return diagnostic.json()["id"]


def test_attachment_rejects_disallowed_type_and_oversized_image(client):
    token = register(client, "upload-validation@example.com")["access_token"]
    diagnostic_id = create_diagnostic(client, token)
    endpoint = f"/api/v1/diagnostics/{diagnostic_id}/attachments"

    wrong_type = client.post(
        endpoint,
        headers=auth(token),
        files={"file": ("notes.txt", b"not an image", "text/plain")},
    )
    assert wrong_type.status_code == 415

    too_large = client.post(
        endpoint,
        headers=auth(token),
        files={"file": ("fault.png", b"x" * (MAX_ATTACHMENT_BYTES + 1), "image/png")},
    )
    assert too_large.status_code == 413
    assert client.get(endpoint, headers=auth(token)).json() == []


def test_attachment_ownership_random_storage_and_delete(client):
    owner = register(client, "attachment-owner@example.com")["access_token"]
    stranger = register(client, "attachment-stranger@example.com")["access_token"]
    diagnostic_id = create_diagnostic(client, owner)
    endpoint = f"/api/v1/diagnostics/{diagnostic_id}/attachments"

    uploaded = client.post(
        endpoint,
        headers=auth(owner),
        files={"file": ("fault-photo.jpg", image_bytes("JPEG"), "image/jpeg")},
    )
    assert uploaded.status_code == 201, uploaded.text
    attachment = uploaded.json()
    assert attachment["original_filename"] == "fault-photo.jpg"
    stored_files = list(client.app.state.attachment_dir.iterdir())
    assert len(stored_files) == 1
    assert stored_files[0].name != "fault-photo.jpg"
    assert stored_files[0].suffix == ".jpg"

    assert client.get(endpoint, headers=auth(stranger)).status_code == 403
    assert client.post(
        endpoint,
        headers=auth(stranger),
        files={"file": ("other.png", b"png test data", "image/png")},
    ).status_code == 403
    delete_endpoint = f"{endpoint}/{attachment['id']}"
    assert client.delete(delete_endpoint, headers=auth(stranger)).status_code == 403

    listed = client.get(endpoint, headers=auth(owner))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [attachment["id"]]
    assert client.delete(delete_endpoint, headers=auth(owner)).status_code == 204
    assert client.get(endpoint, headers=auth(owner)).json() == []
    assert list(client.app.state.attachment_dir.iterdir()) == []


def test_unresolved_report_contains_attachment_filenames(client):
    token = register(client, "attachment-report@example.com")["access_token"]
    diagnostic_id = create_diagnostic(client, token)
    endpoint = f"/api/v1/diagnostics/{diagnostic_id}/attachments"
    for filename, content_type, image_format in (
        ("fault-screen.png", "image/png", "PNG"),
        ("brush-photo.webp", "image/webp", "WEBP"),
    ):
        uploaded = client.post(
            endpoint,
            headers=auth(token),
            files={"file": (filename, image_bytes(image_format), content_type)},
        )
        assert uploaded.status_code == 201, uploaded.text

    for _ in range(3):
        feedback = submit_feedback(client, token, diagnostic_id, "not_resolved")
        assert feedback.status_code == 200, feedback.text

    report = client.post(f"/api/v1/diagnostics/{diagnostic_id}/report", headers=auth(token))
    assert report.status_code == 201, report.text
    assert "附件文件：" in report.json()["content"]
    assert "fault-screen.png" in report.json()["content"]
    assert "brush-photo.webp" in report.json()["content"]


def test_attachment_rejects_fake_image_and_format_mismatch(client):
    token = register(client, "upload-signature@example.com")["access_token"]
    diagnostic_id = create_diagnostic(client, token)
    endpoint = f"/api/v1/diagnostics/{diagnostic_id}/attachments"

    fake = client.post(
        endpoint,
        headers=auth(token),
        files={"file": ("fake.png", b"not really a png", "image/png")},
    )
    assert fake.status_code == 415

    mismatched = client.post(
        endpoint,
        headers=auth(token),
        files={"file": ("actually-png.jpg", image_bytes("PNG"), "image/jpeg")},
    )
    assert mismatched.status_code == 415


def test_attachment_rejects_excessive_pixels(client):
    token = register(client, "upload-pixels@example.com")["access_token"]
    diagnostic_id = create_diagnostic(client, token)
    endpoint = f"/api/v1/diagnostics/{diagnostic_id}/attachments"
    output = BytesIO()
    Image.new("1", (5001, 5001)).save(output, format="PNG")

    response = client.post(
        endpoint,
        headers=auth(token),
        files={"file": ("huge-pixels.png", output.getvalue(), "image/png")},
    )
    assert response.status_code == 413


def test_attachment_limit_and_finished_session_upload_block(client):
    token = register(client, "upload-lifecycle@example.com")["access_token"]
    diagnostic_id = create_diagnostic(client, token)
    endpoint = f"/api/v1/diagnostics/{diagnostic_id}/attachments"

    for index in range(5):
        response = client.post(
            endpoint,
            headers=auth(token),
            files={"file": (f"photo-{index}.png", image_bytes("PNG"), "image/png")},
        )
        assert response.status_code == 201, response.text
    sixth = client.post(
        endpoint,
        headers=auth(token),
        files={"file": ("photo-6.png", image_bytes("PNG"), "image/png")},
    )
    assert sixth.status_code == 409

    for _ in range(3):
        assert submit_feedback(client, token, diagnostic_id, "not_resolved").status_code == 200
    ended = client.post(
        endpoint,
        headers=auth(token),
        files={"file": ("after-end.png", image_bytes("PNG"), "image/png")},
    )
    assert ended.status_code == 409


def test_concurrent_attachment_uploads_enforce_limit_without_orphans(client):
    token = register(client, "upload-concurrent-limit@example.com")["access_token"]
    diagnostic_id = create_diagnostic(client, token)
    endpoint = f"/api/v1/diagnostics/{diagnostic_id}/attachments"

    for index in range(4):
        response = client.post(
            endpoint,
            headers=auth(token),
            files={"file": (f"existing-{index}.png", image_bytes("PNG"), "image/png")},
        )
        assert response.status_code == 201, response.text

    def upload(index: int):
        return client.post(
            endpoint,
            headers=auth(token),
            files={"file": (f"concurrent-{index}.png", image_bytes("PNG"), "image/png")},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(upload, range(2)))

    assert sorted(response.status_code for response in responses) == [201, 409]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json()["detail"] == "A diagnostic can contain at most five images"

    with client.app.state.session_factory() as db:
        attachments = list(
            db.scalars(select(Attachment).where(Attachment.session_id == diagnostic_id))
        )
    stored_names = {attachment.stored_filename for attachment in attachments}
    physical_names = {
        path.name for path in client.app.state.attachment_dir.iterdir() if path.is_file()
    }
    assert len(attachments) == 5
    assert physical_names == stored_names


def test_failed_physical_delete_is_tracked_for_cleanup(client, monkeypatch):
    token = register(client, "upload-delete-tracking@example.com")["access_token"]
    diagnostic_id = create_diagnostic(client, token)
    endpoint = f"/api/v1/diagnostics/{diagnostic_id}/attachments"
    uploaded = client.post(
        endpoint,
        headers=auth(token),
        files={"file": ("delete-me.png", image_bytes("PNG"), "image/png")},
    ).json()

    original_unlink = type(client.app.state.attachment_dir).unlink

    def failing_unlink(path, *args, **kwargs):
        if path.name.startswith(".pending-delete-"):
            raise PermissionError("simulated file lock")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(client.app.state.attachment_dir), "unlink", failing_unlink)
    response = client.delete(f"{endpoint}/{uploaded['id']}", headers=auth(token))
    assert response.status_code == 204

    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Attachment)) == 0
        pending = db.scalar(select(PendingFileDeletion))
        assert pending is not None
        assert "simulated file lock" in (pending.last_error or "")
        assert (client.app.state.attachment_dir / pending.quarantined_filename).is_file()
