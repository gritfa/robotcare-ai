from functools import lru_cache

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


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/robotcare.db"
    environment: str = "development"
    jwt_secret: str = DEFAULT_DEVELOPMENT_JWT_SECRET
    access_token_minutes: int = Field(default=60, ge=1, le=1440)
    refresh_token_days: int = Field(default=14, ge=1, le=90)
    refresh_reuse_grace_seconds: int = Field(default=5, ge=0, le=30)
    refresh_cookie_secure: bool | None = None
    login_max_failures: int = Field(default=5, ge=1, le=100)
    login_window_minutes: int = Field(default=15, ge=1, le=1440)
    login_lock_minutes: int = Field(default=15, ge=1, le=1440)
    cors_origins: str = "http://localhost:5173"
    attachment_dir: str = "./data/attachments"
    report_dir: str = "./data/reports"
    dashscope_api_key: str | None = None
    auto_create_schema: bool = False

    model_config = SettingsConfigDict(env_prefix="ROBOTCARE_", env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.environment.strip().lower() != "production":
            return self
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

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def effective_refresh_cookie_secure(self) -> bool:
        if self.refresh_cookie_secure is not None:
            return self.refresh_cookie_secure
        return self.environment.strip().lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
