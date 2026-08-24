from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import get_settings
from app.models import AIProviderConfig
from conftest import auth, register
from test_admin_api import make_admin


def _enable_encryption(monkeypatch) -> str:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(get_settings(), "ai_config_encryption_key", key)
    return key


def test_ai_config_requires_full_admin(client):
    token = register(client, "ordinary-ai-config@example.com")["access_token"]
    assert client.get("/api/v1/admin/ai-config", headers=auth(token)).status_code == 403
    assert (
        client.patch(
            "/api/v1/admin/ai-config/status",
            headers=auth(token),
            json={"enabled": False},
        ).status_code
        == 403
    )


def test_admin_can_save_encrypted_keys_and_never_read_them_back(client, monkeypatch):
    _enable_encryption(monkeypatch)
    token = make_admin(client, "ai-config-admin@example.com")
    response = client.put(
        "/api/v1/admin/ai-config",
        headers=auth(token),
        json={
            "llm_backend": "openai-compat",
            "generation_model": "qwen3.7-max",
            "generation_base_url": "https://example-model.invalid/compatible-mode/v1",
            "embedding_base_url": "https://example-model.invalid/api/v1",
            "generation_api_key": "secret-generation-key",
            "reuse_generation_key_for_embedding": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["generation_api_key_configured"] is True
    assert body["embedding_api_key_configured"] is True
    assert "secret-generation-key" not in response.text

    with client.app.state.session_factory() as db:
        row = db.scalar(select(AIProviderConfig).where(AIProviderConfig.id == 1))
        assert row is not None
        assert row.generation_api_key_encrypted != "secret-generation-key"
        assert row.embedding_api_key_encrypted != "secret-generation-key"
        assert row.generation_api_key_encrypted.startswith("gAAAA")

    reread = client.get("/api/v1/admin/ai-config", headers=auth(token))
    assert reread.status_code == 200
    assert "secret-generation-key" not in reread.text


def test_admin_can_disable_and_enable_runtime_without_restart(client, monkeypatch):
    _enable_encryption(monkeypatch)
    token = make_admin(client, "ai-toggle-admin@example.com")
    saved = client.put(
        "/api/v1/admin/ai-config",
        headers=auth(token),
        json={
            "llm_backend": "openai-compat",
            "generation_model": "qwen3.7-max",
            "generation_base_url": "https://example-model.invalid/compatible-mode/v1",
            "embedding_base_url": "https://example-model.invalid/api/v1",
            "generation_api_key": "secret-generation-key",
            "embedding_api_key": "secret-embedding-key",
        },
    )
    assert saved.status_code == 200, saved.text

    class ReachabilityProbe:
        def __init__(self):
            self.clear_calls = 0

        def clear(self):
            self.clear_calls += 1

    probe = ReachabilityProbe()
    client.app.state.embedding_reachability = probe

    disabled = client.patch(
        "/api/v1/admin/ai-config/status",
        headers=auth(token),
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["runtime_ready"] is False
    assert client.app.state.generation_provider.model_name == "disabled"
    assert probe.clear_calls == 1

    enabled = client.patch(
        "/api/v1/admin/ai-config/status",
        headers=auth(token),
        json={"enabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True
    assert enabled.json()["runtime_ready"] is True
    assert client.app.state.generation_provider.model_name == "qwen3.7-max"
    assert probe.clear_calls == 2


def test_ai_config_rejects_private_or_non_https_base_urls(client, monkeypatch):
    _enable_encryption(monkeypatch)
    token = make_admin(client, "ai-url-admin@example.com")
    for base_url in ("http://model.example.com/v1", "https://127.0.0.1/v1"):
        response = client.put(
            "/api/v1/admin/ai-config",
            headers=auth(token),
            json={
                "llm_backend": "openai-compat",
                "generation_model": "qwen3.7-max",
                "generation_base_url": base_url,
                "generation_api_key": "key",
                "embedding_api_key": "key",
            },
        )
        assert response.status_code == 422


def test_production_rejects_api_keys_over_plain_http(client, monkeypatch):
    _enable_encryption(monkeypatch)
    token = make_admin(client, "ai-transport-admin@example.com")
    monkeypatch.setattr(get_settings(), "environment", "production")

    response = client.put(
        "/api/v1/admin/ai-config",
        headers=auth(token),
        json={
            "llm_backend": "openai-compat",
            "generation_model": "qwen3.7-max",
            "generation_base_url": "https://model.example.com/v1",
            "generation_api_key": "must-not-cross-plain-http",
            "embedding_api_key": "must-not-cross-plain-http",
        },
    )

    assert response.status_code == 403
    assert "HTTPS" in response.json()["detail"]
    assert "must-not-cross-plain-http" not in response.text


def test_connection_test_uses_saved_config_and_writes_no_secret(monkeypatch, client):
    _enable_encryption(monkeypatch)
    token = make_admin(client, "ai-test-admin@example.com")
    saved = client.put(
        "/api/v1/admin/ai-config",
        headers=auth(token),
        json={
            "llm_backend": "dashscope",
            "generation_model": "qwen-plus",
            "generation_api_key": "connection-secret",
            "reuse_generation_key_for_embedding": True,
        },
    )
    assert saved.status_code == 200

    class Embedding:
        def embed_query(self, text):
            assert text == "连接测试"
            return [1.0]

    class Generation:
        def generate(self, *, system, prompt):
            assert system and prompt
            return "OK"

    monkeypatch.setattr(
        "app.routers.ai_config.build_runtime_providers",
        lambda config, settings: (Embedding(), Generation()),
    )
    response = client.post("/api/v1/admin/ai-config/test", headers=auth(token))
    assert response.status_code == 200
    assert response.json() == {
        "generation_service": True,
        "embedding_service": True,
        "errors": [],
    }
    assert "connection-secret" not in response.text
