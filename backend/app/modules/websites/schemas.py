"""Theme token schema (stored in website_themes.tokens).

Kept deliberately small and explicit so the frontend can map tokens to CSS variables.
New optional fields can be added without migrations.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    computed_field,
    model_validator,
)

from app.core.hostnames import InvalidHostname, normalize_hostname
from app.core.locale import InvalidLocale, normalize_locale


def _normalize_locale(value: str) -> str:
    try:
        return normalize_locale(value)
    except InvalidLocale as exc:
        raise ValueError(str(exc)) from exc


def _normalize_host(value: str) -> str:
    try:
        return normalize_hostname(value)
    except InvalidHostname as exc:
        raise ValueError(str(exc)) from exc


HexColor = Field(pattern=r"^#(?:[0-9a-fA-F]{3}){1,2}$")


class ThemeColors(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: str = HexColor
    primary_foreground: str = HexColor
    secondary: str = HexColor
    accent: str = HexColor
    background: str = HexColor
    foreground: str = HexColor
    muted: str = HexColor
    border: str = HexColor


class ThemeFont(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family: str = Field(min_length=1, max_length=100)
    weights: list[int] = Field(default_factory=lambda: [400, 700])
    source: Literal["google", "system", "self_hosted"] = "google"


class ThemeFonts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: ThemeFont
    body: ThemeFont


class ThemeTokens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    colors: ThemeColors
    fonts: ThemeFonts
    radius: Literal["none", "sm", "md", "lg", "full"] = "md"
    header_style: Literal["minimal", "centered", "split"] = "minimal"
    product_card_style: Literal["plain", "bordered", "elevated"] = "bordered"


# Neutral default for new websites; editors replace it via PUT /theme.
DEFAULT_THEME: dict[str, object] = {
    "colors": {
        "primary": "#111827",
        "primary_foreground": "#FFFFFF",
        "secondary": "#F3F4F6",
        "accent": "#2563EB",
        "background": "#FFFFFF",
        "foreground": "#111827",
        "muted": "#6B7280",
        "border": "#E5E7EB",
    },
    "fonts": {"heading": {"family": "Inter"}, "body": {"family": "Inter"}},
}


# ======================================================================================
# Admin API schemas
# ======================================================================================

WebsiteKey = Annotated[
    str, Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$", examples=["nordic-home-at"])
]
CountryCode = Annotated[str, Field(pattern=r"^[A-Za-z]{2}$"), AfterValidator(str.upper)]
Locale = Annotated[str, AfterValidator(_normalize_locale), Field(examples=["de"])]


def _check_locales(default_locale: str | None, locales: list[str] | None) -> None:
    if locales is not None:
        if not locales:
            raise ValueError("locales must not be empty")
        if len(set(locales)) != len(locales):
            raise ValueError("locales must be unique")
    if default_locale is not None and locales is not None and default_locale not in locales:
        raise ValueError("default_locale must be one of locales")


class WebsiteCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "key": "nordic-home-at",
                    "name": "Nordic Home Österreich",
                    "country_code": "AT",
                    "default_locale": "de",
                    "locales": ["de", "en"],
                    "auto_publish": True,
                }
            ]
        },
    )

    key: WebsiteKey
    name: str = Field(min_length=1, max_length=200)
    country_code: CountryCode
    default_locale: Locale
    locales: list[Locale] = Field(min_length=1, max_length=20)
    auto_publish: bool = True
    theme: ThemeTokens | None = None

    @model_validator(mode="after")
    def _locales(self) -> "WebsiteCreate":
        _check_locales(self.default_locale, self.locales)
        return self


class WebsiteUpdate(BaseModel):
    """Partial update. `key` is immutable (it is the public cache/URL identifier)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"name": "Nordic Home Austria", "locales": ["de", "en"]}]},
    )

    name: str | None = Field(default=None, min_length=1, max_length=200)
    country_code: CountryCode | None = None
    default_locale: Locale | None = None
    locales: list[Locale] | None = Field(default=None, min_length=1, max_length=20)
    auto_publish: bool | None = None

    @model_validator(mode="after")
    def _locales(self) -> "WebsiteUpdate":
        _check_locales(None, self.locales)
        return self


class WebsiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    name: str
    country_code: str
    default_locale: str
    locales: list[str]
    status: str
    auto_publish: bool
    content_version: int
    published_version: int
    published_at: datetime | None
    primary_host: str | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_unpublished_changes(self) -> bool:
        return self.published_version < self.content_version


class DomainCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "hostname": "nordic-home.at",
                    "is_primary": True,
                    "redirect_to_primary": True,
                    "cloudflare_zone_id": "023e105f4ecef8ad9ca31a8372d0c353",
                }
            ]
        },
    )

    hostname: Annotated[str, Field(min_length=1, max_length=260), AfterValidator(_normalize_host)]
    is_primary: bool = False
    redirect_to_primary: bool = True
    cloudflare_zone_id: str | None = Field(default=None, max_length=64)


class DomainUpdate(BaseModel):
    """Activation happens only through verification; `status` can disable or reset to
    pending (which requires verifying again)."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"redirect_to_primary": False}]}
    )

    redirect_to_primary: bool | None = None
    cloudflare_zone_id: str | None = Field(default=None, max_length=64)
    status: Literal["pending", "disabled"] | None = None


class DomainVerification(BaseModel):
    record_type: Literal["TXT"] = "TXT"
    record_name: str
    value: str


class DomainOut(BaseModel):
    id: uuid.UUID
    website_id: uuid.UUID
    hostname: str
    is_primary: bool
    status: str
    redirect_to_primary: bool
    cloudflare_zone_id: str | None
    verified_at: datetime | None
    verification: DomainVerification


class ThemeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tokens: ThemeTokens
    logo_key: str | None
    logo_dark_key: str | None
    favicon_key: str | None
    og_default_image_key: str | None
    version: int
    updated_at: datetime


class ThemeUpdate(BaseModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"tokens": cast(JsonValue, DEFAULT_THEME)}]}
    )

    tokens: ThemeTokens


class PublishJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    website_id: uuid.UUID
    status: str
    tags: list[str]
    paths: list[str]
    full_site: bool
    content_version: int
    attempts: int
    run_after: datetime
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
