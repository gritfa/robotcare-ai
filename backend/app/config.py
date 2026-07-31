from functools import lru_cache
from ipaddress import ip_network
import json
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_DEVELOPMENT_JWT_SECRET = "development-secret-change-before-deploying"
INSECURE_PRODUCTION_JWT_SECRETS = {
    DEFAULT_DEVELOPMENT_JWT_SECRET,
    "replace-with-a-long-random-secret",
    "replace-this-with-a-long-random-secret",
    "change-me",
}
INSECURE_PRODUCTION_SECRET_MARKERS = ("replace", "change-me", "example", "test-only")
INSECURE_INVITE_SECRET_MARKERS = (
    "replace",
    "change-me",
    "example",
    "test-only",
    "placeholder",
)


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://robotcare:robotcare@localhost:5432/robotcare"
    environment: str = "development"
    jwt_secret: str = DEFAULT_DEVELOPMENT_JWT_SECRET
    registration_mode: Literal["open", "invite", "closed"] = "open"
    registration_invite_secret: str | None = None
    access_token_minutes: int = Field(default=60, ge=1, le=1440)
    refresh_token_days: int = Field(default=14, ge=1, le=90)
    refresh_reuse_grace_seconds: int = Field(default=5, ge=0, le=30)
    refresh_cookie_secure: bool | None = None
    login_max_failures: int = Field(default=5, ge=1, le=100)
    login_email_max_failures: int = Field(default=20, ge=1, le=1000)
    login_ip_max_failures: int = Field(default=30, ge=1, le=5000)
    login_window_minutes: int = Field(default=15, ge=1, le=1440)
    login_lock_minutes: int = Field(default=15, ge=1, le=1440)
    trusted_proxy_cidrs: str = ""
    registration_email_per_minute: int = Field(default=3, ge=1, le=1000)
    registration_ip_per_minute: int = Field(default=10, ge=1, le=5000)
    knowledge_search_user_per_minute: int = Field(default=20, ge=1, le=5000)
    knowledge_search_ip_per_minute: int = Field(default=60, ge=1, le=10000)
    diagnostic_create_user_per_minute: int = Field(default=10, ge=1, le=1000)
    diagnostic_create_ip_per_minute: int = Field(default=30, ge=1, le=5000)
    attachment_upload_user_per_minute: int = Field(default=20, ge=1, le=1000)
    attachment_upload_ip_per_minute: int = Field(default=60, ge=1, le=5000)
    report_create_user_per_minute: int = Field(default=5, ge=1, le=1000)
    report_create_ip_per_minute: int = Field(default=15, ge=1, le=5000)
    pdf_create_user_per_minute: int = Field(default=5, ge=1, le=1000)
    pdf_create_ip_per_minute: int = Field(default=15, ge=1, le=5000)
    embedding_user_per_minute: int = Field(default=10, ge=1, le=5000)
    embedding_ip_per_minute: int = Field(default=30, ge=1, le=10000)
    embedding_user_per_day: int = Field(default=100, ge=1, le=100000)
    embedding_ip_per_day: int = Field(default=300, ge=1, le=500000)
    knowledge_answer_user_per_minute: int = Field(default=6, ge=1, le=1000)
    knowledge_answer_ip_per_minute: int = Field(default=20, ge=1, le=5000)
    generation_model: str = "qwen-plus"
    alert_webhook_url: str | None = None
    alert_webhook_format: str = "wecom"  # wecom | dingtalk | generic
    alert_cooldown_seconds: int = Field(default=14400, ge=60, le=86400)
    knowledge_search_cache_ttl_seconds: int = Field(default=30, ge=1, le=300)
    cors_origins: str = "http://localhost:5173"
    attachment_dir: str = "./data/attachments"
    report_dir: str = "./data/reports"
    dashscope_api_key: str | None = None
    dashscope_base_url: str | None = None
    llm_backend: str = "dashscope"  # dashscope | openai-compat
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    knowledge_min_score: float = Field(default=0.25, ge=0, le=1)
    knowledge_min_score_overrides: str = "{}"
    auto_create_schema: bool = False

    model_config = SettingsConfigDict(env_prefix="ROBOTCARE_", env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.llm_backend not in ("dashscope", "openai-compat"):
            raise ValueError("ROBOTCARE_LLM_BACKEND must be dashscope or openai-compat")
        if self.llm_backend == "openai-compat" and not (self.llm_base_url or "").strip():
            raise ValueError(
                "ROBOTCARE_LLM_BASE_URL is required when ROBOTCARE_LLM_BACKEND=openai-compat"
            )
        try:
            self.trusted_proxy_networks
        except ValueError as exc:
            raise ValueError(
                "ROBOTCARE_TRUSTED_PROXY_CIDRS must be a comma-separated list of IP networks"
            ) from exc
        try:
            overrides = json.loads(self.knowledge_min_score_overrides)
        except json.JSONDecodeError as exc:
            raise ValueError("ROBOTCARE_KNOWLEDGE_MIN_SCORE_OVERRIDES must be valid JSON") from exc
        if not isinstance(overrides, dict) or any(
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= float(value) <= 1
            for key, value in overrides.items()
        ):
            raise ValueError(
                "ROBOTCARE_KNOWLEDGE_MIN_SCORE_OVERRIDES must map model or "
                "model:knowledge-version keys to scores between 0 and 1"
            )
        if self.registration_mode == "invite":
            invite_secret = (self.registration_invite_secret or "").strip()
            lowered_invite_secret = invite_secret.lower()
            if len(invite_secret) < 16 or any(
                marker in lowered_invite_secret for marker in INSECURE_INVITE_SECRET_MARKERS
            ):
                raise ValueError(
                    "ROBOTCARE_REGISTRATION_INVITE_SECRET must be at least 16 characters "
                    "and must not use a documented placeholder in invite mode"
                )
        if self.environment.strip().lower() != "production":
            return self
        if self.registration_mode == "open":
            raise ValueError("ROBOTCARE_REGISTRATION_MODE must not be open in production")
        if not (self.dashscope_api_key or "").strip():
            raise ValueError(
                "ROBOTCARE_DASHSCOPE_API_KEY is required in production; "
                "knowledge capability must not start silently disabled"
            )
        if self.dashscope_base_url and not self.dashscope_base_url.startswith("https://"):
            raise ValueError("ROBOTCARE_DASHSCOPE_BASE_URL must use HTTPS in production")
        secret = self.jwt_secret.strip()
        lowered_secret = secret.lower()
        if (
            len(secret) < 32
            or lowered_secret in INSECURE_PRODUCTION_JWT_SECRETS
            or any(marker in lowered_secret for marker in INSECURE_PRODUCTION_SECRET_MARKERS)
        ):
            raise ValueError(
                "ROBOTCARE_JWT_SECRET must be at least 32 characters and must not use "
                "a documented default in production"
            )
        if self.auto_create_schema:
            raise ValueError("ROBOTCARE_AUTO_CREATE_SCHEMA must be false in production")
        if self.refresh_cookie_secure is False:
            raise ValueError("ROBOTCARE_REFRESH_COOKIE_SECURE must not be false in production")
        return self

    def knowledge_score_threshold(
        self, model_code: str, knowledge_version: str | None = None
    ) -> float:
        overrides = json.loads(self.knowledge_min_score_overrides)
        if knowledge_version is not None:
            version_key = f"{model_code}:{knowledge_version}"
            if version_key in overrides:
                return float(overrides[version_key])
        if model_code in overrides:
            return float(overrides[model_code])
        return self.knowledge_min_score

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def trusted_proxy_networks(self):
        return tuple(
            ip_network(item.strip(), strict=False)
            for item in self.trusted_proxy_cidrs.split(",")
            if item.strip()
        )

    def business_rate_limits(self, action: str) -> tuple[int, int]:
        limits = {
            "knowledge_search": (
                self.knowledge_search_user_per_minute,
                self.knowledge_search_ip_per_minute,
            ),
            "knowledge_answer": (
                self.knowledge_answer_user_per_minute,
                self.knowledge_answer_ip_per_minute,
            ),
            "diagnostic_create": (
                self.diagnostic_create_user_per_minute,
                self.diagnostic_create_ip_per_minute,
            ),
            "attachment_upload": (
                self.attachment_upload_user_per_minute,
                self.attachment_upload_ip_per_minute,
            ),
            "report_create": (
                self.report_create_user_per_minute,
                self.report_create_ip_per_minute,
            ),
            "pdf_create": (
                self.pdf_create_user_per_minute,
                self.pdf_create_ip_per_minute,
            ),
        }
        try:
            return limits[action]
        except KeyError as exc:
            raise ValueError(f"Unsupported rate-limited action: {action}") from exc

    @property
    def effective_refresh_cookie_secure(self) -> bool:
        if self.refresh_cookie_secure is not None:
            return self.refresh_cookie_secure
        return self.environment.strip().lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
