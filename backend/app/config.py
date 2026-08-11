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
    # 连接池此前完全用 SQLAlchemy 默认值（5 + 10 溢出，无 pre_ping、无 recycle）。
    # SSE 聊天在生成期间会占用数据库连接，池容量不足会影响登录和健康检查。
    # 池大小要按「并发聊天数 + 常规请求」估，不是按 CPU 核数。
    db_pool_size: int = Field(default=20, ge=1, le=200)
    db_max_overflow: int = Field(default=30, ge=0, le=200)
    db_pool_timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0)
    # 回收早于任何中间件的空闲断连阈值；没有 pre_ping 时 Postgres 重启后
    # 第一批请求会全部 500。
    db_pool_recycle_seconds: int = Field(default=1800, ge=60, le=86400)
    db_pool_pre_ping: bool = True
    environment: str = "development"
    jwt_secret: str = DEFAULT_DEVELOPMENT_JWT_SECRET
    # jwt_secret 此前被复用为三种用途（JWT 签名 / 限流键 HMAC / 报告文件名派生），
    # 轮换它会同时踢掉所有会话、清零限流计数、让已发出的报告链接全部失效。
    # 拆成独立密钥；未配置时回退 jwt_secret 以兼容既有部署，
    # 回退状态会在启动时告警（main.py），不静默。
    rate_limit_hmac_secret: str | None = None
    report_filename_secret: str | None = None
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
    # 登录成功也要限流：失败计数只拦得住猜密码的人，拦不住拿着有效凭据狂刷
    # 令牌的脚本；每次成功登录都会签发 token 并写入 refresh_tokens。
    login_success_email_per_minute: int = Field(default=10, ge=1, le=1000)
    login_success_ip_per_minute: int = Field(default=30, ge=1, le=5000)
    # 每个用户的会话总数上限。会话表此前无上限，一个脚本可以无限建空会话
    max_conversations_per_user: int = Field(default=200, ge=1, le=10000)
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
    knowledge_search_cache_max_entries: int = Field(default=512, ge=16, le=100000)
    citation_page_cache_max_entries: int = Field(default=64, ge=1, le=10000)
    citation_page_cache_max_mb: int = Field(default=32, ge=1, le=1024)
    citation_page_max_concurrent: int = Field(default=4, ge=1, le=64)
    readiness_probe_success_ttl_seconds: int = Field(default=300, ge=10, le=3600)
    readiness_probe_failure_ttl_seconds: int = Field(default=30, ge=5, le=600)
    cors_origins: str = "http://localhost:5173"
    attachment_dir: str = "./data/attachments"
    report_dir: str = "./data/reports"
    knowledge_dir: str = "./data/knowledge"
    dashscope_api_key: str | None = None
    dashscope_base_url: str | None = None
    llm_backend: str = "dashscope"  # dashscope | openai-compat
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    # 外部模型超时预算。默认值必须小于 frontend/nginx.conf 的 proxy_read_timeout
    # （当前 60s），否则用户已收到 504、后端还在跑并照常计费；
    # 该约束由 scripts/verify_deployment_config.py 静态断言守住。
    llm_connect_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
    llm_read_timeout_seconds: float = Field(default=40.0, ge=5.0, le=600.0)
    llm_budget_seconds: float = Field(default=50.0, ge=5.0, le=600.0)
    llm_max_attempts: int = Field(default=2, ge=1, le=4)
    # embedding 是短请求（一批文本向量化），预算远小于生成
    embedding_connect_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
    embedding_read_timeout_seconds: float = Field(default=20.0, ge=5.0, le=600.0)
    embedding_budget_seconds: float = Field(default=30.0, ge=5.0, le=600.0)
    embedding_max_attempts: int = Field(default=2, ge=1, le=4)
    # 每百万 token 单价（元）。默认按 qwen-plus 公开价填，换模型必须跟着改——
    # 单价必须随模型配置更新，避免成本统计失真。
    # 形如 {"qwen-plus": {"prompt": 0.8, "completion": 2.0}}，按模型名精确匹配。
    llm_token_prices: str = "{}"
    llm_default_prompt_price_per_million: float = Field(default=0.8, ge=0, le=10000)
    llm_default_completion_price_per_million: float = Field(default=2.0, ge=0, le=10000)
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
        # 预算必须容得下连接加一段可用的读取，否则每次调用都在预算耗尽处失败
        if self.llm_budget_seconds < self.llm_connect_timeout_seconds + 5:
            raise ValueError(
                "ROBOTCARE_LLM_BUDGET_SECONDS must exceed "
                "ROBOTCARE_LLM_CONNECT_TIMEOUT_SECONDS by at least 5 seconds"
            )
        if self.embedding_budget_seconds < self.embedding_connect_timeout_seconds + 5:
            raise ValueError(
                "ROBOTCARE_EMBEDDING_BUDGET_SECONDS must exceed "
                "ROBOTCARE_EMBEDDING_CONNECT_TIMEOUT_SECONDS by at least 5 seconds"
            )
        try:
            token_prices = json.loads(self.llm_token_prices)
        except json.JSONDecodeError as exc:
            raise ValueError("ROBOTCARE_LLM_TOKEN_PRICES must be valid JSON") from exc
        if not isinstance(token_prices, dict):
            raise ValueError(
                "ROBOTCARE_LLM_TOKEN_PRICES must map model names to "
                '{"prompt": <price>, "completion": <price>} per million tokens'
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

    def token_price_per_million(self, model: str) -> tuple[float, float]:
        """返回 (输入单价, 输出单价)，单位：元 / 百万 token。

        找不到该模型的配置就用默认值——宁可按默认价估算，也不要因为没配价格
        就把成本记成 0：一个恒为 0 的成本看板比没有看板更危险。
        """
        try:
            prices = json.loads(self.llm_token_prices)
        except json.JSONDecodeError:
            prices = {}
        entry = prices.get(model) if isinstance(prices, dict) else None
        if isinstance(entry, dict):
            return (
                float(entry.get("prompt", self.llm_default_prompt_price_per_million)),
                float(entry.get("completion", self.llm_default_completion_price_per_million)),
            )
        return (
            self.llm_default_prompt_price_per_million,
            self.llm_default_completion_price_per_million,
        )

    @property
    def effective_rate_limit_hmac_secret(self) -> str:
        """限流键 HMAC 用的密钥。轮换它只影响当前限流窗口，不踢用户下线。"""
        return (self.rate_limit_hmac_secret or "").strip() or self.jwt_secret

    @property
    def effective_report_filename_secret(self) -> str:
        """报告文件名派生用的密钥。轮换它会让旧报告链接失效，故必须独立于 JWT。"""
        return (self.report_filename_secret or "").strip() or self.jwt_secret

    @property
    def reused_jwt_secret_purposes(self) -> list[str]:
        """仍在复用 JWT 密钥的用途清单，供启动告警使用。"""
        purposes = []
        if not (self.rate_limit_hmac_secret or "").strip():
            purposes.append("rate_limit_hmac")
        if not (self.report_filename_secret or "").strip():
            purposes.append("report_filename")
        return purposes

    @property
    def llm_timeout_policy(self):
        from .llm_transport import TimeoutPolicy

        return TimeoutPolicy(
            connect_seconds=self.llm_connect_timeout_seconds,
            read_seconds=self.llm_read_timeout_seconds,
            budget_seconds=self.llm_budget_seconds,
            max_attempts=self.llm_max_attempts,
        )

    @property
    def embedding_timeout_policy(self):
        from .llm_transport import TimeoutPolicy

        return TimeoutPolicy(
            connect_seconds=self.embedding_connect_timeout_seconds,
            read_seconds=self.embedding_read_timeout_seconds,
            budget_seconds=self.embedding_budget_seconds,
            max_attempts=self.embedding_max_attempts,
        )

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
