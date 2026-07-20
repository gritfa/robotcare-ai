from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/robotcare.db"
    jwt_secret: str = "development-secret-change-before-deploying"
    access_token_minutes: int = 60
    cors_origins: str = "http://localhost:5173"
    attachment_dir: str = "./data/attachments"
    report_dir: str = "./data/reports"
    dashscope_api_key: str | None = None
    auto_create_schema: bool = False

    model_config = SettingsConfigDict(env_prefix="ROBOTCARE_", env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
