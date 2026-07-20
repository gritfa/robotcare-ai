from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.auth_service import (
    REFRESH_COOKIE_NAME,
    record_login_failure,
    rotate_refresh_token,
    set_refresh_cookie,
)
from app.config import Settings
from app.models import AuthSession, LoginThrottle, RefreshToken, User
from conftest import auth, register


PASSWORD = "StrongPass123"


def _refresh_cookie(client) -> str:
    value = client.cookies.get(REFRESH_COOKIE_NAME)
    assert value
    return value


def _set_refresh_cookie(client, value: str) -> None:
    client.cookies.set(REFRESH_COOKIE_NAME, value, path="/api/v1/auth")


def test_register_sets_opaque_httponly_refresh_cookie_and_keeps_body_compatible(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "cookie@example.com", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert {"access_token", "token_type", "user"} <= payload.keys()
    assert "refresh_token" not in payload

    set_cookie = response.headers["set-cookie"].lower()
    assert f"{REFRESH_COOKIE_NAME}=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/api/v1/auth" in set_cookie
    assert "secure" not in set_cookie  # development default only

    raw_refresh = _refresh_cookie(client)
    assert len(raw_refresh) >= 60
    session_factory = client.app.state.session_factory
    with session_factory() as db:
        stored = list(db.scalars(select(RefreshToken)))
        assert len(stored) == 1
        assert len(stored[0].token_hash) == 64
        assert stored[0].token_hash != raw_refresh
        assert raw_refresh not in repr(stored[0].__dict__)


def test_refresh_grace_handles_immediate_reuse_then_late_replay_revokes_session(client):
    created = register(client, "rotation@example.com")
    old_access = created["access_token"]
    old_refresh = _refresh_cookie(client)

    rotated = client.post("/api/v1/auth/refresh")
    assert rotated.status_code == 200, rotated.text
    new_access = rotated.json()["access_token"]
    new_refresh = _refresh_cookie(client)
    assert new_refresh != old_refresh
    assert rotated.request.content == b""

    assert client.get("/api/v1/auth/me", headers=auth(old_access)).status_code == 200
    assert client.get("/api/v1/auth/me", headers=auth(new_access)).status_code == 200

    _set_refresh_cookie(client, old_refresh)
    concurrent_retry = client.post("/api/v1/auth/refresh")
    assert concurrent_retry.status_code == 409
    assert int(concurrent_retry.headers["Retry-After"]) >= 1
    assert client.get("/api/v1/auth/me", headers=auth(old_access)).status_code == 200
    assert client.get("/api/v1/auth/me", headers=auth(new_access)).status_code == 200

    session_factory = client.app.state.session_factory
    with session_factory() as db:
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash.is_not(None), RefreshToken.used_at.is_not(None))
            .values(used_at=datetime.now(timezone.utc) - timedelta(seconds=31))
        )
        db.commit()

    replay = client.post("/api/v1/auth/refresh")
    assert replay.status_code == 401
    assert "replay" in replay.json()["detail"].lower()
    assert client.get("/api/v1/auth/me", headers=auth(old_access)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=auth(new_access)).status_code == 401

    with session_factory() as db:
        auth_session = db.scalar(select(AuthSession))
        assert auth_session is not None
        assert auth_session.revoked_at is not None
        assert auth_session.revoke_reason == "refresh_replay"


def test_concurrent_refresh_allows_one_consumer_and_grace_keeps_session_active(client):
    access = register(client, "concurrent-refresh@example.com")["access_token"]
    raw_refresh = _refresh_cookie(client)
    session_factory = client.app.state.session_factory

    def rotate_once():
        with session_factory() as db:
            try:
                _user, issued = rotate_refresh_token(db, raw_refresh)
                return 200, issued.access_token
            except HTTPException as exc:
                return exc.status_code, exc.detail

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: rotate_once(), range(2)))

    assert sorted(result[0] for result in results) == [200, 409]
    assert client.get("/api/v1/auth/me", headers=auth(access)).status_code == 200
    successful_access = next(result[1] for result in results if result[0] == 200)
    assert client.get("/api/v1/auth/me", headers=auth(successful_access)).status_code == 200


def test_atomic_login_failure_upsert_does_not_lose_concurrent_counts(client):
    session_factory = client.app.state.session_factory
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": [],
            "client": ("127.0.0.1", 50000),
        }
    )
    settings = Settings(login_max_failures=100, _env_file=None)

    def record_once(_index: int) -> None:
        with session_factory() as db:
            record_login_failure(db, "atomic@example.com", request, settings)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(record_once, range(8)))

    with session_factory() as db:
        rows = list(db.scalars(select(LoginThrottle)))
        assert {row.scope for row in rows} == {"email", "email_ip"}
        assert all(row.failure_count == 8 for row in rows)


def test_concurrent_login_api_enforces_threshold_before_extra_password_checks(client):
    register(client, "concurrent-limit@example.com")
    client.post("/api/v1/auth/logout")

    def wrong_login(_index: int) -> int:
        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "concurrent-limit@example.com",
                "password": "wrong-password",
            },
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=8) as executor:
        status_codes = list(executor.map(wrong_login, range(8)))

    assert status_codes.count(401) == 4
    assert status_codes.count(429) == 4

    session_factory = client.app.state.session_factory
    with session_factory() as db:
        rows = list(
            db.scalars(
                select(LoginThrottle).where(LoginThrottle.failure_count == 5)
            )
        )
        assert {row.scope for row in rows} == {"email", "email_ip"}


def test_login_password_length_is_bounded_before_authentication_work(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "bounded@example.com", "password": "x" * 129},
    )
    assert response.status_code == 422

    session_factory = client.app.state.session_factory
    with session_factory() as db:
        assert list(db.scalars(select(LoginThrottle))) == []


def test_logout_has_no_body_deletes_cookie_and_invalidates_existing_access(client):
    access = register(client, "logout@example.com")["access_token"]
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 204
    assert response.content == b""
    set_cookie = response.headers["set-cookie"].lower()
    assert f"{REFRESH_COOKIE_NAME}=" in set_cookie
    assert "max-age=0" in set_cookie
    assert "path=/api/v1/auth" in set_cookie
    assert client.get("/api/v1/auth/me", headers=auth(access)).status_code == 401


def test_logout_can_revoke_the_access_session_when_refresh_cookie_is_missing(client):
    access = register(client, "logout-without-cookie@example.com")["access_token"]
    client.cookies.clear()

    response = client.post("/api/v1/auth/logout", headers=auth(access))

    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/api/v1/auth/me", headers=auth(access)).status_code == 401


def test_disabled_user_cannot_login_refresh_or_use_existing_access(client):
    access = register(client, "disabled@example.com")["access_token"]
    session_factory = client.app.state.session_factory
    with session_factory() as db:
        user = db.scalar(select(User).where(User.email == "disabled@example.com"))
        assert user is not None
        user.status = "disabled"
        db.commit()

    denied_login = client.post(
        "/api/v1/auth/login",
        json={"email": "disabled@example.com", "password": PASSWORD},
    )
    assert denied_login.status_code == 403
    assert client.get("/api/v1/auth/me", headers=auth(access)).status_code == 403
    denied_refresh = client.post("/api/v1/auth/refresh")
    assert denied_refresh.status_code == 403


def test_login_limit_is_database_backed_account_wide_and_clears_after_success(client):
    register(client, "limited@example.com")
    client.post("/api/v1/auth/logout")

    for attempt in range(1, 5):
        response = client.post(
            "/api/v1/auth/login",
            headers={"X-Forwarded-For": f"198.51.100.{attempt}"},
            json={"email": "LIMITED@example.com", "password": "wrong-password"},
        )
        assert response.status_code == 401

    locked = client.post(
        "/api/v1/auth/login",
        headers={"X-Forwarded-For": "203.0.113.77"},
        json={"email": "limited@example.com", "password": "wrong-password"},
    )
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0
    assert client.post(
        "/api/v1/auth/login",
        json={"email": "limited@example.com", "password": PASSWORD},
    ).status_code == 429

    session_factory = client.app.state.session_factory
    with session_factory() as db:
        rows = list(db.scalars(select(LoginThrottle)))
        assert {row.scope for row in rows} == {"email", "email_ip"}
        assert all(row.failure_count == 5 for row in rows)
        assert all(row.client_ip_hash != "testclient" for row in rows)
        assert "testclient" not in repr([row.__dict__ for row in rows])

    register(client, "clear-limit@example.com")
    client.post("/api/v1/auth/logout")
    for _ in range(2):
        assert client.post(
            "/api/v1/auth/login",
            json={"email": "clear-limit@example.com", "password": "wrong-password"},
        ).status_code == 401
    assert client.post(
        "/api/v1/auth/login",
        json={"email": "clear-limit@example.com", "password": PASSWORD},
    ).status_code == 200
    with session_factory() as db:
        # Only the still-locked first account remains. The successful login
        # removed both throttle scopes for clear-limit@example.com.
        assert len(list(db.scalars(select(LoginThrottle)))) == 2


def test_register_integrity_error_is_reported_as_conflict(client, monkeypatch):
    session_class = client.app.state.session_factory.class_

    def raise_integrity_error(*args, **kwargs):
        del args, kwargs
        raise IntegrityError("insert users", {}, RuntimeError("unique collision"))

    monkeypatch.setattr(session_class, "flush", raise_integrity_error)
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "race@example.com", "password": PASSWORD},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Email already registered"


def test_production_settings_reject_weak_secret_or_unsafe_schema_and_default_secure_cookie():
    common = {"environment": "production", "_env_file": None}
    for overrides in (
        {"jwt_secret": "short"},
        {"jwt_secret": "development-secret-change-before-deploying"},
        {"jwt_secret": "replace-with-at-least-32-random-characters"},
        {"jwt_secret": "s" * 40, "auto_create_schema": True},
        {"jwt_secret": "s" * 40, "refresh_cookie_secure": False},
    ):
        try:
            Settings(**common, **overrides)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe production settings accepted: {overrides}")

    settings = Settings(**common, jwt_secret="s" * 40)
    assert settings.effective_refresh_cookie_secure is True
    response = Response()
    set_refresh_cookie(response, "opaque-token", settings)
    assert "secure" in response.headers["set-cookie"].lower()


def test_access_jwt_contains_server_session_and_access_type(client):
    import jwt

    access = register(client, "claims@example.com")["access_token"]
    settings = Settings(_env_file=None)
    payload = jwt.decode(access, settings.jwt_secret, algorithms=["HS256"])
    assert payload["type"] == "access"
    assert isinstance(payload["sid"], str) and payload["sid"]
    assert payload["sub"].isdigit()
    assert datetime.fromtimestamp(payload["exp"], timezone.utc) > datetime.now(timezone.utc)
