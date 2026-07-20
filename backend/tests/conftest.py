import pytest
from fastapi.testclient import TestClient
from io import BytesIO
from PIL import Image

from app.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(
        f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        attachment_dir=tmp_path / "attachments",
        report_dir=tmp_path / "reports",
        auto_create_schema=True,
    )
    with TestClient(app) as test_client:
        yield test_client


def register(client: TestClient, email: str) -> dict:
    response = client.post("/api/v1/auth/register", json={"email": email, "password": "StrongPass123"})
    assert response.status_code == 201, response.text
    return response.json()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def submit_feedback(client: TestClient, token: str, diagnostic_id: int, outcome: str):
    step = client.get(
        f"/api/v1/diagnostics/{diagnostic_id}/steps/current", headers=auth(token)
    )
    assert step.status_code == 200, step.text
    return client.post(
        f"/api/v1/diagnostics/{diagnostic_id}/feedback",
        headers=auth(token),
        json={"step_id": step.json()["id"], "outcome": outcome},
    )


def image_bytes(image_format: str = "PNG", size: tuple[int, int] = (16, 16)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color=(32, 120, 90)).save(output, format=image_format)
    return output.getvalue()
