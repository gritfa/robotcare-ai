from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

import app.api as api_module
import app.auth_service as auth_service_module
from app.auth_service import (
    REFRESH_COOKIE_NAME,
    cleanup_expired_login_throttles,
    client_ip,
    login_attempt_lock_ids,
    login_throttle_keys,
    login_throttle_limit,
    record_login_failure,
    rotate_refresh_token,
    set_refresh_cookie,
)
from app.config import Settings
from app.models import AuthSession, LoginThrottle, RefreshToken, User
from app.security import DUMMY_PASSWORD_HASH, verify_password as verify_real_password
from conftest import auth, register


PASSWORD = "StrongPass123"
DUMMY_PASSWORD = "robotcare-dummy-password-never-used"


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
        assert {row.scope for row in rows} == {"email", "email_ip", "ip"}
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
        assert {row.scope for row in rows} == {"email", "email_ip", "ip"}


def test_login_password_length_is_bounded_before_authentication_work(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "bounded@example.com", "password": "x" * 129},
    )
    assert response.status_code == 422

    session_factory = client.app.state.session_factory
    with session_factory() as db:
        assert list(db.scalars(select(LoginThrottle))) == []


def test_wrong_password_and_unknown_user_each_verify_once_with_identical_response(
    client, monkeypatch
):
    register(client, "timing-existing@example.com")
    client.post("/api/v1/auth/logout")

    session_factory = client.app.state.session_factory
    with session_factory() as db:
        existing = db.scalar(
            select(User).where(User.email == "timing-existing@example.com")
        )
        assert existing is not None
        existing_password_hash = existing.password_hash

    verified_hashes: list[str] = []

    def reject_password(_password: str, password_hash: str) -> bool:
        verified_hashes.append(password_hash)
        return False

    monkeypatch.setattr(api_module, "verify_password", reject_password)
    trace_headers = {"X-Request-ID": "7dc61147-996c-4e99-b434-7c66690a8397"}

    existing_response = client.post(
        "/api/v1/auth/login",
        headers=trace_headers,
        json={
            "email": "timing-existing@example.com",
            "password": "wrong-password",
        },
    )
    assert verified_hashes == [existing_password_hash]

    verified_hashes.clear()
    unknown_response = client.post(
        "/api/v1/auth/login",
        headers=trace_headers,
        json={
            "email": "timing-unknown@example.com",
            "password": "wrong-password",
        },
    )
    assert verified_hashes == [DUMMY_PASSWORD_HASH]
    assert unknown_response.status_code == existing_response.status_code == 401
    assert unknown_response.json() == existing_response.json()
    assert unknown_response.json()["detail"] == "Invalid email or password"


def test_dummy_password_hash_is_a_valid_argon2_hash():
    assert verify_real_password(DUMMY_PASSWORD, DUMMY_PASSWORD_HASH) is True


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
        assert {row.scope for row in rows} == {"email", "email_ip", "ip"}
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
        remaining = list(db.scalars(select(LoginThrottle)))
        assert {row.scope for row in remaining} == {"email", "email_ip", "ip"}
        assert sum(row.scope == "ip" for row in remaining) == 1


def test_login_scope_limits_are_independent_and_keys_do_not_store_plaintext():
    settings = Settings(
        login_max_failures=5,
        login_email_max_failures=20,
        login_ip_max_failures=30,
        _env_file=None,
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": [],
            "client": ("198.51.100.44", 50000),
        }
    )
    keys = login_throttle_keys("Private@Example.com", request, settings)
    assert {key.scope: login_throttle_limit(key, settings) for key in keys} == {
        "email": 20,
        "email_ip": 5,
        "ip": 30,
    }
    assert keys[2].email_hash is None
    assert keys[2].client_ip_hash is not None
    assert "private@example.com" not in repr(keys).lower()
    assert "198.51.100.44" not in repr(keys)


def test_trusted_proxy_resolution_ignores_spoofing_and_walks_chain_right_to_left():
    untrusted_settings = Settings(trusted_proxy_cidrs="10.0.0.10/32", _env_file=None)
    spoofed = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"203.0.113.99")],
            "client": ("198.51.100.7", 50000),
        }
    )
    assert client_ip(spoofed, untrusted_settings) == "198.51.100.7"

    trusted_settings = Settings(
        trusted_proxy_cidrs="10.0.0.0/24,192.0.2.10/32", _env_file=None
    )
    forwarded = Request(
        {
            "type": "http",
            "headers": [
                (b"x-forwarded-for", b"203.0.113.8, 192.0.2.10")
            ],
            "client": ("10.0.0.10", 50000),
        }
    )
    assert client_ip(forwarded, trusted_settings) == "203.0.113.8"

    malformed = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"attacker-controlled")],
            "client": ("10.0.0.10", 50000),
        }
    )
    assert client_ip(malformed, trusted_settings) == "10.0.0.10"


def test_two_hop_proxy_chain_keeps_the_real_client_and_ignores_spoofed_prefix():
    """宿主 HTTPS 反代 → 容器 Nginx → 后端：真实客户端必须活到限流桶里。

    Nginx 从覆盖式 X-Forwarded-For 改成 $proxy_add_x_forwarded_for 之后，
    链上会有两个可信跳。原来的覆盖式写法在这个拓扑下把所有人压成同一个地址，
    任意一人失败登录 30 次就能锁掉全站（2026-08-06 体检 D1）。
    """

    settings = Settings(trusted_proxy_cidrs="172.30.0.0/24", _env_file=None)

    # 上游反代把真实客户端写进链首，容器 Nginx 追加自己看到的上游地址
    two_hops = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"203.0.113.44, 172.30.0.1")],
            "client": ("172.30.0.10", 50000),
        }
    )
    assert client_ip(two_hops, settings) == "203.0.113.44"

    # 同一拓扑下换一个真实客户端，必须落到不同的桶——否则限流形同虚设
    other_client = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"203.0.113.45, 172.30.0.1")],
            "client": ("172.30.0.10", 50000),
        }
    )
    assert client_ip(other_client, settings) != client_ip(two_hops, settings)

    # 调用方自己塞的值只会排在链最左边，右到左走链永远取不到它
    spoofed_prefix = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"9.9.9.9, 203.0.113.44, 172.30.0.1")],
            "client": ("172.30.0.10", 50000),
        }
    )
    assert client_ip(spoofed_prefix, settings) == "203.0.113.44"


def test_empty_trusted_proxy_config_falls_back_to_the_peer_instead_of_trusting_headers():
    """没配可信网段时宁可退化成「全站一个桶」，也不能采信调用方的头。

    这个退化本身是不可接受的运维状态（登录限流会被一个人拖垮），
    由 scripts/verify_deployment_config.py 在部署前拦下；这里锁住的是
    「退化的方向必须是保守的」——不能变成人人自选限流桶。
    """

    settings = Settings(trusted_proxy_cidrs="", _env_file=None)
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", b"203.0.113.44, 172.30.0.1")],
            "client": ("172.30.0.10", 50000),
        }
    )
    assert client_ip(request, settings) == "172.30.0.10"


def test_postgres_login_lock_ids_are_ordered_and_share_email_or_ip_boundaries():
    settings = Settings(_env_file=None)

    def request_from(address: str) -> Request:
        return Request(
            {
                "type": "http",
                "headers": [],
                "client": (address, 50000),
            }
        )

    first = login_attempt_lock_ids(
        "first@example.com", request_from("198.51.100.1"), settings
    )
    same_ip = login_attempt_lock_ids(
        "second@example.com", request_from("198.51.100.1"), settings
    )
    same_email = login_attempt_lock_ids(
        "first@example.com", request_from("198.51.100.2"), settings
    )
    assert first == tuple(sorted(first))
    assert len(set(first) & set(same_ip)) == 1
    assert len(set(first) & set(same_email)) == 1


def test_concurrent_login_rotating_emails_is_stopped_by_independent_ip_bucket(
    client, monkeypatch
):
    settings = Settings(
        login_max_failures=100,
        login_email_max_failures=100,
        login_ip_max_failures=5,
        _env_file=None,
    )
    monkeypatch.setattr(auth_service_module, "get_settings", lambda: settings)

    def unknown_login(index: int) -> int:
        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": f"rotating-{index}@example.com",
                "password": "wrong-password",
            },
        )
        if response.status_code == 429:
            assert int(response.headers["Retry-After"]) >= 1
        return response.status_code

    with ThreadPoolExecutor(max_workers=8) as executor:
        status_codes = list(executor.map(unknown_login, range(8)))

    assert status_codes.count(401) == 4
    assert status_codes.count(429) == 4
    session_factory = client.app.state.session_factory
    with session_factory() as db:
        ip_rows = list(
            db.scalars(select(LoginThrottle).where(LoginThrottle.scope == "ip"))
        )
        assert len(ip_rows) == 1
        assert ip_rows[0].failure_count == 5


def test_cleanup_expired_login_throttles_preserves_active_and_locked_rows(client):
    now = datetime.now(timezone.utc)
    session_factory = client.app.state.session_factory
    with session_factory() as db:
        db.add_all(
            [
                LoginThrottle(
                    key_hash="a" * 64,
                    scope="email",
                    email_hash="b" * 64,
                    client_ip_hash=None,
                    failure_count=1,
                    window_started_at=now - timedelta(hours=2),
                    locked_until=None,
                    updated_at=now - timedelta(hours=2),
                ),
                LoginThrottle(
                    key_hash="c" * 64,
                    scope="ip",
                    email_hash=None,
                    client_ip_hash="d" * 64,
                    failure_count=1,
                    window_started_at=now,
                    locked_until=None,
                    updated_at=now,
                ),
                LoginThrottle(
                    key_hash="e" * 64,
                    scope="email_ip",
                    email_hash="f" * 64,
                    client_ip_hash="1" * 64,
                    failure_count=5,
                    window_started_at=now - timedelta(hours=2),
                    locked_until=now + timedelta(minutes=5),
                    updated_at=now - timedelta(hours=2),
                ),
            ]
        )
        db.commit()
        assert cleanup_expired_login_throttles(db, now=now) == 1
        assert {row.key_hash for row in db.scalars(select(LoginThrottle))} == {
            "c" * 64,
            "e" * 64,
        }


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
    common = {
        "environment": "production",
        "registration_mode": "closed",
        "dashscope_api_key": "production-embedding-key",
        "_env_file": None,
    }
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
