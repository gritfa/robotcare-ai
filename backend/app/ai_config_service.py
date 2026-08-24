from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from types import SimpleNamespace
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from .generation_service import build_generation_provider
from .knowledge_service import DashScopeEmbeddingProvider, EMBEDDING_MODEL
from .models import AIProviderConfig, utcnow


class AIConfigurationError(RuntimeError):
    pass


class DisabledEmbeddingProvider:
    model_name = "disabled"

    def embed_documents(self, texts):
        del texts
        raise RuntimeError("AI service is disabled by administrator")

    def embed_query(self, text):
        del text
        raise RuntimeError("AI service is disabled by administrator")


class DisabledGenerationProvider:
    model_name = "disabled"

    def generate(self, *, system: str, prompt: str) -> str:
        del system, prompt
        raise RuntimeError("AI service is disabled by administrator")

    def generate_stream(self, *, system: str, prompt: str):
        del system, prompt
        raise RuntimeError("AI service is disabled by administrator")
        yield ""  # pragma: no cover - keeps this method an iterator


@dataclass(frozen=True)
class EffectiveAIConfig:
    enabled: bool
    source: str
    llm_backend: str
    generation_model: str
    generation_base_url: str | None
    embedding_base_url: str | None
    generation_api_key: str | None
    embedding_api_key: str | None
    updated_at: object | None = None


def _clean(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def validate_public_https_url(value: str | None, *, required: bool, field: str) -> str | None:
    normalized = _clean(value)
    if normalized is None:
        if required:
            raise AIConfigurationError(f"{field} is required")
        return None
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AIConfigurationError(f"{field} must be a public HTTPS URL without credentials")
    try:
        address = ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    ):
        raise AIConfigurationError(f"{field} must not target a private or local address")
    return normalized.rstrip("/")


def _fernet(settings) -> Fernet:
    key = _clean(getattr(settings, "ai_config_encryption_key", None))
    if key is None:
        raise AIConfigurationError(
            "ROBOTCARE_AI_CONFIG_ENCRYPTION_KEY is not configured on the server"
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise AIConfigurationError(
            "ROBOTCARE_AI_CONFIG_ENCRYPTION_KEY is not a valid Fernet key"
        ) from exc


def encrypt_secret(value: str, settings) -> str:
    return _fernet(settings).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str | None, settings) -> str | None:
    if not value:
        return None
    try:
        return _fernet(settings).decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise AIConfigurationError("Saved AI credentials cannot be decrypted") from exc


def database_config(db: Session) -> AIProviderConfig | None:
    return db.scalar(select(AIProviderConfig).where(AIProviderConfig.id == 1))


def effective_config(db: Session, settings, *, ignore_enabled: bool = False) -> EffectiveAIConfig:
    row = database_config(db)
    if row is None:
        generation_key = (
            settings.llm_api_key
            if settings.llm_backend == "openai-compat"
            else settings.dashscope_api_key
        )
        embedding_key = settings.dashscope_api_key
        enabled = bool(_clean(generation_key) and _clean(embedding_key))
        return EffectiveAIConfig(
            enabled=True if ignore_enabled else enabled,
            source="environment",
            llm_backend=settings.llm_backend,
            generation_model=settings.generation_model,
            generation_base_url=(
                _clean(settings.llm_base_url)
                if settings.llm_backend == "openai-compat"
                else _clean(settings.dashscope_base_url)
            ),
            embedding_base_url=_clean(settings.dashscope_base_url),
            generation_api_key=_clean(generation_key),
            embedding_api_key=_clean(embedding_key),
        )

    generation_key = decrypt_secret(row.generation_api_key_encrypted, settings) or _clean(
        settings.llm_api_key
        if row.llm_backend == "openai-compat"
        else settings.dashscope_api_key
    )
    embedding_key = decrypt_secret(row.embedding_api_key_encrypted, settings) or _clean(
        settings.dashscope_api_key
    )
    return EffectiveAIConfig(
        enabled=True if ignore_enabled else row.enabled,
        source="database",
        llm_backend=row.llm_backend,
        generation_model=row.generation_model,
        generation_base_url=_clean(row.generation_base_url),
        embedding_base_url=_clean(row.embedding_base_url),
        generation_api_key=generation_key,
        embedding_api_key=embedding_key,
        updated_at=row.updated_at,
    )


def validate_effective_config(config: EffectiveAIConfig) -> None:
    if not _clean(config.generation_api_key):
        raise AIConfigurationError("Generation API key is not configured")
    if not _clean(config.embedding_api_key):
        raise AIConfigurationError("Embedding API key is not configured")
    validate_public_https_url(
        config.generation_base_url,
        required=config.llm_backend == "openai-compat",
        field="generation_base_url",
    )
    validate_public_https_url(
        config.embedding_base_url,
        required=False,
        field="embedding_base_url",
    )


def build_runtime_providers(config: EffectiveAIConfig, settings):
    validate_effective_config(config)
    provider_settings = SimpleNamespace(
        llm_backend=config.llm_backend,
        llm_api_key=config.generation_api_key,
        dashscope_api_key=config.generation_api_key,
        generation_model=config.generation_model,
        llm_base_url=config.generation_base_url,
        dashscope_base_url=config.generation_base_url,
        llm_timeout_policy=settings.llm_timeout_policy,
    )
    generation_provider = build_generation_provider(provider_settings)
    embedding_provider = DashScopeEmbeddingProvider(
        config.embedding_api_key,
        config.embedding_base_url,
        timeout_policy=settings.embedding_timeout_policy,
    )
    return embedding_provider, generation_provider


def apply_runtime_config(application, db: Session, settings) -> EffectiveAIConfig:
    config = effective_config(db, settings)
    reachability = getattr(application.state, "embedding_reachability", None)
    if reachability is not None:
        reachability.clear()
    if not config.enabled:
        application.state.embedding_provider = DisabledEmbeddingProvider()
        application.state.generation_provider = DisabledGenerationProvider()
        application.state.embedding_configured = False
        application.state.generation_configured = False
        application.state.ai_runtime_ready = False
        cache = getattr(application.state, "knowledge_search_cache", None)
        if cache is not None:
            cache.clear()
        return config
    embedding_provider, generation_provider = build_runtime_providers(config, settings)
    application.state.embedding_provider = embedding_provider
    application.state.generation_provider = generation_provider
    application.state.embedding_configured = True
    application.state.generation_configured = True
    application.state.ai_runtime_ready = True
    cache = getattr(application.state, "knowledge_search_cache", None)
    if cache is not None:
        cache.clear()
    return config


def save_config(db: Session, payload, settings, *, admin_id: int) -> AIProviderConfig:
    row = database_config(db)
    if row is None:
        current = effective_config(db, settings)
        row = AIProviderConfig(
            id=1,
            enabled=current.enabled,
            llm_backend=current.llm_backend,
            generation_model=current.generation_model,
            generation_base_url=current.generation_base_url,
            embedding_base_url=current.embedding_base_url,
        )
        db.add(row)

    generation_base_url = validate_public_https_url(
        payload.generation_base_url,
        required=payload.llm_backend == "openai-compat",
        field="generation_base_url",
    )
    embedding_base_url = validate_public_https_url(
        payload.embedding_base_url,
        required=False,
        field="embedding_base_url",
    )
    row.llm_backend = payload.llm_backend
    row.generation_model = payload.generation_model.strip()
    row.generation_base_url = generation_base_url
    row.embedding_base_url = embedding_base_url

    generation_key = (
        payload.generation_api_key.get_secret_value().strip()
        if payload.generation_api_key is not None
        else ""
    )
    embedding_key = (
        payload.embedding_api_key.get_secret_value().strip()
        if payload.embedding_api_key is not None
        else ""
    )
    if payload.clear_generation_api_key:
        row.generation_api_key_encrypted = None
    elif generation_key:
        row.generation_api_key_encrypted = encrypt_secret(generation_key, settings)
    if payload.clear_embedding_api_key:
        row.embedding_api_key_encrypted = None
    elif payload.reuse_generation_key_for_embedding and generation_key:
        row.embedding_api_key_encrypted = encrypt_secret(generation_key, settings)
    elif embedding_key:
        row.embedding_api_key_encrypted = encrypt_secret(embedding_key, settings)
    row.updated_by = admin_id
    row.updated_at = utcnow()
    db.flush()
    return row


def set_enabled(db: Session, enabled: bool, settings, *, admin_id: int) -> AIProviderConfig:
    row = database_config(db)
    if row is None:
        current = effective_config(db, settings)
        row = AIProviderConfig(
            id=1,
            enabled=False,
            llm_backend=current.llm_backend,
            generation_model=current.generation_model,
            generation_base_url=current.generation_base_url,
            embedding_base_url=current.embedding_base_url,
        )
        db.add(row)
        db.flush()
    row.enabled = enabled
    row.updated_by = admin_id
    row.updated_at = utcnow()
    db.flush()
    if enabled:
        validate_effective_config(effective_config(db, settings))
    return row


def config_status(db: Session, settings, *, runtime_ready: bool) -> dict[str, object]:
    row = database_config(db)
    if row is None:
        config = effective_config(db, settings)
        generation_configured = bool(config.generation_api_key)
        embedding_configured = bool(config.embedding_api_key)
    else:
        generation_secret_in_database = bool(row.generation_api_key_encrypted)
        embedding_secret_in_database = bool(row.embedding_api_key_encrypted)
        generation_configured = bool(
            generation_secret_in_database
            or _clean(
                settings.llm_api_key
                if row.llm_backend == "openai-compat"
                else settings.dashscope_api_key
            )
        )
        embedding_configured = bool(
            embedding_secret_in_database or _clean(settings.dashscope_api_key)
        )
        config = EffectiveAIConfig(
            enabled=row.enabled,
            source=(
                "database"
                if generation_secret_in_database and embedding_secret_in_database
                else "mixed"
            ),
            llm_backend=row.llm_backend,
            generation_model=row.generation_model,
            generation_base_url=_clean(row.generation_base_url),
            embedding_base_url=_clean(row.embedding_base_url),
            generation_api_key=None,
            embedding_api_key=None,
            updated_at=row.updated_at,
        )
    return {
        "enabled": config.enabled,
        "runtime_ready": runtime_ready,
        "source": config.source,
        "llm_backend": config.llm_backend,
        "generation_model": config.generation_model,
        "generation_base_url": config.generation_base_url,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_base_url": config.embedding_base_url,
        "generation_api_key_configured": generation_configured,
        "embedding_api_key_configured": embedding_configured,
        "encryption_ready": bool(_clean(getattr(settings, "ai_config_encryption_key", None))),
        "updated_at": config.updated_at,
    }
