import pytest
from pydantic import ValidationError

import app.api as api_module
from app.config import Settings


PASSWORD = "StrongPass123"
VALID_INVITE_SECRET = "7cYp9N2mK4qR8vTx"
DENIED_DETAIL = "Registration is not available"


def _settings(mode: str, invite_secret: str | None = None) -> Settings:
    return Settings(
        registration_mode=mode,
        registration_invite_secret=invite_secret,
        _env_file=None,
    )


def _register(client, *, email: str, invite_code: str | None = None):
    payload = {"email": email, "password": PASSWORD}
    if invite_code is not None:
        payload["invite_code"] = invite_code
    return client.post("/api/v1/auth/register", json=payload)


def test_open_registration_remains_backward_compatible(client, monkeypatch):
    monkeypatch.setattr(api_module, "get_settings", lambda: _settings("open"))

    response = _register(client, email="open-registration@example.com")

    assert response.status_code == 201
    assert response.json()["user"]["email"] == "open-registration@example.com"


def test_invite_registration_accepts_correct_code(client, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "get_settings",
        lambda: _settings("invite", VALID_INVITE_SECRET),
    )

    response = _register(
        client,
        email="invited@example.com",
        invite_code=VALID_INVITE_SECRET,
    )

    assert response.status_code == 201


@pytest.mark.parametrize("invite_code", [None, "", "incorrect-invite-code", "错误邀请码"])
def test_invite_registration_rejects_missing_or_wrong_code_without_reason(
    client,
    monkeypatch,
    invite_code,
):
    monkeypatch.setattr(
        api_module,
        "get_settings",
        lambda: _settings("invite", VALID_INVITE_SECRET),
    )

    response = _register(
        client,
        email="denied@example.com",
        invite_code=invite_code,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == DENIED_DETAIL


def test_closed_registration_returns_generic_forbidden(client, monkeypatch):
    monkeypatch.setattr(api_module, "get_settings", lambda: _settings("closed"))

    response = _register(
        client,
        email="closed@example.com",
        invite_code=VALID_INVITE_SECRET,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == DENIED_DETAIL


def test_invite_code_request_length_is_bounded(client, monkeypatch):
    monkeypatch.setattr(api_module, "get_settings", lambda: _settings("open"))

    response = _register(
        client,
        email="oversized-code@example.com",
        invite_code="x" * 129,
    )

    assert response.status_code == 422


def test_production_rejects_open_registration():
    with pytest.raises(ValidationError, match="must not be open"):
        Settings(
            environment="production",
            jwt_secret="s" * 40,
            dashscope_api_key="production-embedding-key",
            registration_mode="open",
            _env_file=None,
        )


def test_production_rejects_missing_embedding_configuration():
    with pytest.raises(ValidationError, match="DASHSCOPE_API_KEY"):
        Settings(
            environment="production",
            jwt_secret="s" * 40,
            registration_mode="closed",
            dashscope_api_key=None,
            _env_file=None,
        )


@pytest.mark.parametrize(
    "invite_secret",
    [
        None,
        "too-short",
        "replace-with-random-value",
        "change-me-to-a-secret",
        "example-invite-secret",
        "test-only-invite-secret",
        "placeholder-secret-value",
    ],
)
def test_invite_mode_rejects_missing_short_or_placeholder_secret(invite_secret):
    with pytest.raises(ValidationError, match="REGISTRATION_INVITE_SECRET"):
        Settings(
            registration_mode="invite",
            registration_invite_secret=invite_secret,
            _env_file=None,
        )


def test_production_accepts_invite_or_closed_registration_with_secure_settings():
    invited = Settings(
        environment="production",
        jwt_secret="s" * 40,
        dashscope_api_key="production-embedding-key",
        registration_mode="invite",
        registration_invite_secret=VALID_INVITE_SECRET,
        _env_file=None,
    )
    closed = Settings(
        environment="production",
        jwt_secret="s" * 40,
        dashscope_api_key="production-embedding-key",
        registration_mode="closed",
        _env_file=None,
    )

    assert invited.registration_mode == "invite"
    assert closed.registration_mode == "closed"
