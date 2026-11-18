"""Environment-based configuration.

All settings come from environment variables (see docker-compose.yml / .env.prod.example).
The app fails fast at startup when a required variable is missing or invalid.
"""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

AppEnv = Literal["local", "test", "production"]


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # --- app ---
    app_env: AppEnv = "local"
    app_version: str = "dev"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    docs_enabled: bool = True
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Non-upload request bodies (JSON). Uploads are limited by UPLOAD_MAX_BYTES.
    max_request_bytes: int = Field(default=1024 * 1024, ge=1024)
    # Per client IP and API process; put a Cloudflare rate-limit rule in front as well.
    login_rate_limit_per_minute: int = Field(default=10, ge=1)

    # --- database ---
    database_url: str
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    db_pool_timeout_seconds: float = 10.0
    db_statement_timeout_ms: int = Field(default=15_000, ge=0)
    # Public reads are simple indexed queries; fail fast instead of piling up connections.
    public_statement_timeout_ms: int = Field(default=5_000, ge=0)
    db_echo: bool = False

    # --- auth ---
    jwt_secret: SecretStr
    jwt_access_ttl_minutes: int = Field(default=30, ge=1, le=24 * 60)

    # --- object storage (SeaweedFS locally, Cloudflare R2 in prod) ---
    storage_endpoint_url: str
    storage_region: str = "auto"
    storage_access_key_id: str
    storage_secret_access_key: SecretStr
    storage_bucket: str
    storage_public_base_url: str
    image_transform_provider: Literal["none", "cloudflare"] = "none"
    upload_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    # Deleted images stay in the bucket this long (cached pages may still reference them).
    media_retention_days: int = Field(default=7, ge=0)
    media_gc_interval_seconds: int = Field(default=3600, ge=60)

    # --- publishing ---
    revalidate_webhook_url: str | None = None
    revalidate_webhook_secret: SecretStr | None = None
    publish_debounce_seconds: int = Field(default=30, ge=0)
    cdn_purge_provider: Literal["none", "cloudflare"] = "none"
    cloudflare_api_token: SecretStr | None = None
    publish_max_attempts: int = Field(default=8, ge=1, le=50)
    # A job "running" longer than this is assumed to belong to a crashed worker.
    publish_job_timeout_seconds: int = Field(default=600, ge=30)
    webhook_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    cloudflare_api_base_url: str = "https://api.cloudflare.com/client/v4"

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// driver")
        return value

    @field_validator("storage_public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _production_guards(self) -> "Settings":
        if self.app_env != "production":
            return self
        secret = self.jwt_secret.get_secret_value()
        if len(secret) < 32 or "change-me" in secret:
            raise ValueError("JWT_SECRET must be a strong random value (>= 32 chars) in production")
        if self.revalidate_webhook_url and not self.revalidate_webhook_secret:
            raise ValueError("REVALIDATE_WEBHOOK_SECRET is required when a webhook URL is set")
        if self.cdn_purge_provider == "cloudflare" and not self.cloudflare_api_token:
            raise ValueError("CLOUDFLARE_API_TOKEN is required when CDN_PURGE_PROVIDER=cloudflare")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
