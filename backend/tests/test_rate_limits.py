from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from sqlalchemy import select

import app.routers.auth as auth_router_module
import app.routers.diagnostics as diagnostics_router_module
import app.routers.knowledge as knowledge_router_module
from app.config import Settings
from app.models import ApiRateLimit
from app.rate_limit_service import (
    RateQuota,
    aggregate_knowledge_version,
    consume_rate_quotas,
    enforce_business_rate_limit,
    enforce_embedding_rate_limit,
    knowledge_cache_key,
    normalize_knowledge_query,
)
from conftest import auth, image_bytes, register, submit_feedback


def request_from(address: str = "198.51.100.20") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/test",
            "headers": [],
            "client": (address, 50000),
        }
    )


def test_user_and_ip_quota_boundary_returns_structured_429_without_plaintext(client):
    settings = Settings(
        diagnostic_create_user_per_minute=2,
        diagnostic_create_ip_per_minute=3,
        _env_file=None,
    )
    session_factory = client.app.state.session_factory
    for _ in range(2):
        with session_factory() as db:
            enforce_business_rate_limit(
                db,
                request_from(),
                action="diagnostic_create",
                user_id=42,
                settings=settings,
            )

    with session_factory() as db:
        try:
            enforce_business_rate_limit(
                db,
                request_from(),
                action="diagnostic_create",
                user_id=42,
                settings=settings,
            )
        except HTTPException as exc:
            assert exc.status_code == 429
            assert exc.detail["code"] == "RATE_LIMITED"
            assert exc.detail["action"] == "diagnostic_create"
            assert exc.detail["scope"] == "user"
            assert int(exc.headers["Retry-After"]) >= 1
        else:
            raise AssertionError("quota boundary did not reject the request")

    with session_factory() as db:
        rows = list(db.scalars(select(ApiRateLimit)))
        assert {row.scope for row in rows} == {"user", "ip"}
        assert all(row.request_count == 2 for row in rows)
        serialized = repr([row.__dict__ for row in rows])
        assert "198.51.100.20" not in serialized
        assert "user_id=42" not in serialized


def test_concurrent_database_quota_does_not_over_admit(client):
    session_factory = client.app.state.session_factory
    settings = Settings(_env_file=None)
    now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    quota = RateQuota("concurrent_test", "ip", "203.0.113.8", "minute", 5)

    def consume_once(_index: int) -> int:
        with session_factory() as db:
            try:
                consume_rate_quotas(db, (quota,), settings, now=now)
            except HTTPException as exc:
                return exc.status_code
            return 200

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(consume_once, range(8)))

    assert statuses.count(200) == 5
    assert statuses.count(429) == 3
    with session_factory() as db:
        row = db.scalar(
            select(ApiRateLimit).where(ApiRateLimit.action == "concurrent_test")
        )
        assert row is not None and row.request_count == 5


def test_registration_uses_email_and_ip_quota_and_returns_retry_after(
    client, monkeypatch
):
    settings = Settings(
        registration_email_per_minute=1,
        registration_ip_per_minute=10,
        _env_file=None,
    )
    monkeypatch.setattr("app.rate_limit_service.get_settings", lambda: settings)
    first = client.post(
        "/api/v1/auth/register",
        json={"email": "registration-limit@example.com", "password": "StrongPass123"},
    )
    assert first.status_code == 201
    blocked = client.post(
        "/api/v1/auth/register",
        json={"email": "REGISTRATION-LIMIT@example.com", "password": "StrongPass123"},
    )
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "RATE_LIMITED"
    assert blocked.json()["detail"]["scope"] == "email"
    assert int(blocked.headers["Retry-After"]) >= 1


def test_registration_ip_quota_stops_rotating_email_addresses(client, monkeypatch):
    settings = Settings(
        registration_email_per_minute=10,
        registration_ip_per_minute=2,
        _env_file=None,
    )
    monkeypatch.setattr("app.rate_limit_service.get_settings", lambda: settings)
    responses = [
        client.post(
            "/api/v1/auth/register",
            json={
                "email": f"rotating-registration-{index}@example.com",
                "password": "StrongPass123",
            },
        )
        for index in range(3)
    ]
    assert [response.status_code for response in responses] == [201, 201, 429]
    assert responses[-1].json()["detail"]["scope"] == "ip"


def test_embedding_enforces_minute_and_day_quotas(client, monkeypatch):
    session_factory = client.app.state.session_factory
    request = request_from()
    first_minute = datetime(2026, 7, 22, 12, 0, 5, tzinfo=timezone.utc)
    monkeypatch.setattr("app.rate_limit_service.utcnow", lambda: first_minute)
    minute_settings = Settings(
        embedding_user_per_minute=1,
        embedding_ip_per_minute=10,
        embedding_user_per_day=100,
        embedding_ip_per_day=100,
        _env_file=None,
    )
    with session_factory() as db:
        enforce_embedding_rate_limit(
            db, request, user_id=77, settings=minute_settings
        )
    with session_factory() as db:
        try:
            enforce_embedding_rate_limit(
                db, request, user_id=77, settings=minute_settings
            )
        except HTTPException as exc:
            assert exc.status_code == 429
            assert exc.detail["action"] == "knowledge_embedding"
            assert exc.detail["window_kind"] == "minute"
        else:
            raise AssertionError("Embedding minute quota did not reject")

    day_settings = Settings(
        embedding_user_per_minute=100,
        embedding_ip_per_minute=100,
        embedding_user_per_day=1,
        embedding_ip_per_day=100,
        _env_file=None,
    )
    monkeypatch.setattr(
        "app.rate_limit_service.utcnow",
        lambda: datetime(2026, 7, 22, 13, 0, 5, tzinfo=timezone.utc),
    )
    with session_factory() as db:
        enforce_embedding_rate_limit(
            db, request_from("198.51.100.21"), user_id=78, settings=day_settings
        )
    monkeypatch.setattr(
        "app.rate_limit_service.utcnow",
        lambda: datetime(2026, 7, 22, 14, 0, 5, tzinfo=timezone.utc),
    )
    with session_factory() as db:
        try:
            enforce_embedding_rate_limit(
                db,
                request_from("198.51.100.21"),
                user_id=78,
                settings=day_settings,
            )
        except HTTPException as exc:
            assert exc.status_code == 429
            assert exc.detail["window_kind"] == "day"
        else:
            raise AssertionError("Embedding daily quota did not reject")


def test_knowledge_cache_key_is_hmac_and_stably_covers_all_inputs():
    settings = Settings(_env_file=None)
    normalized = normalize_knowledge_query("  Cannot   CHARGE  ")
    version = aggregate_knowledge_version(["b" * 64, "a" * 64])
    assert version == aggregate_knowledge_version(["a" * 64, "b" * 64])
    key = knowledge_cache_key(
        settings,
        model_code="JH69U1",
        normalized_query=normalized,
        knowledge_version=version,
        top_k=5,
        min_score=0.25,
    )
    assert len(key) == 64
    assert "cannot charge" not in key
    changed_values = {
        knowledge_cache_key(
            settings,
            model_code=model,
            normalized_query=normalized,
            knowledge_version=knowledge_version,
            top_k=top_k,
            min_score=threshold,
        )
        for model, knowledge_version, top_k, threshold in (
            ("VC35U1", version, 5, 0.25),
            ("JH69U1", "c" * 64, 5, 0.25),
            ("JH69U1", version, 4, 0.25),
            ("JH69U1", version, 5, 0.5),
        )
    }
    assert key not in changed_values
    assert len(changed_values) == 4


def test_all_six_high_cost_routes_are_wired_to_the_expected_limit_action(
    client, monkeypatch
):
    actions: list[str] = []
    registration_calls: list[str] = []

    def record_business(_db, _request, *, action, user_id, settings=None):
        del user_id, settings
        actions.append(action)

    def record_registration(_db, _request, email, settings=None):
        del settings
        registration_calls.append(email)

    monkeypatch.setattr(
        knowledge_router_module, "enforce_business_rate_limit", record_business
    )
    monkeypatch.setattr(
        diagnostics_router_module, "enforce_business_rate_limit", record_business
    )
    monkeypatch.setattr(
        auth_router_module, "enforce_registration_rate_limit", record_registration
    )
    monkeypatch.setattr(
        knowledge_router_module,
        "enforce_embedding_rate_limit",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        knowledge_router_module, "search_knowledge", lambda *args, **kwargs: []
    )

    token = register(client, "wired-routes@example.com")["access_token"]
    assert registration_calls == ["wired-routes@example.com"]
    model_id = next(
        item["id"]
        for item in client.get("/api/v1/models").json()
        if item["code"] == "VC35U1"
    )
    knowledge = client.post(
        "/api/v1/knowledge/search",
        headers=auth(token),
        json={"robot_model_id": model_id, "query": "clean the external brush"},
    )
    assert knowledge.status_code == 200, knowledge.text
    device = client.post(
        "/api/v1/devices",
        headers=auth(token),
        json={"robot_model_id": model_id, "nickname": "Rate limit robot"},
    )
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
    diagnostic_id = diagnostic.json()["id"]
    uploaded = client.post(
        f"/api/v1/diagnostics/{diagnostic_id}/attachments",
        headers=auth(token),
        files={"file": ("fault.png", image_bytes("PNG"), "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    for _ in range(3):
        feedback = submit_feedback(
            client, token, diagnostic_id, "not_resolved"
        )
        assert feedback.status_code == 200, feedback.text
    report = client.post(
        f"/api/v1/diagnostics/{diagnostic_id}/report", headers=auth(token)
    )
    assert report.status_code == 201, report.text
    pdf = client.post(
        f"/api/v1/diagnostics/{diagnostic_id}/report/pdf", headers=auth(token)
    )
    assert pdf.status_code == 201, pdf.text
    assert actions == [
        "knowledge_search",
        "diagnostic_create",
        "attachment_upload",
        "report_create",
        "pdf_create",
    ]
