import pytest
from pydantic import ValidationError

from app.core.config import Settings

BASE = {
    "database_url": "postgresql+asyncpg://u:p@db:5432/x",
    "jwt_secret": "x" * 40,
    "storage_endpoint_url": "http://storage:8333",
    "storage_access_key_id": "k",
    "storage_secret_access_key": "s",
    "storage_bucket": "media",
    "storage_public_base_url": "https://media.example.com/",
}


def make(**overrides: object) -> Settings:
    return Settings(**{**BASE, **overrides})  # type: ignore[arg-type]


def test_cors_origins_parsed_from_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://a.com, https://b.com,")
    settings = make()
    assert settings.cors_allow_origins == ["https://a.com", "https://b.com"]


def test_public_base_url_trailing_slash_stripped() -> None:
    assert make().storage_public_base_url == "https://media.example.com"


def test_requires_asyncpg_driver() -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        make(database_url="postgresql://u:p@db/x")


def test_production_rejects_weak_jwt_secret() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        make(app_env="production", jwt_secret="local-dev-jwt-secret-change-me")


def test_production_requires_webhook_secret_with_url() -> None:
    with pytest.raises(ValidationError, match="REVALIDATE_WEBHOOK_SECRET"):
        make(
            app_env="production",
            revalidate_webhook_url="https://fe.example.com/api/revalidate",
            revalidate_webhook_secret=None,
        )


def test_production_requires_cloudflare_token_for_purge() -> None:
    with pytest.raises(ValidationError, match="CLOUDFLARE_API_TOKEN"):
        make(app_env="production", cdn_purge_provider="cloudflare", cloudflare_api_token=None)


def test_valid_production_settings() -> None:
    settings = make(app_env="production")
    assert settings.is_production
