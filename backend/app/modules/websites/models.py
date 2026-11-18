import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import LOCALE_CHECK_REGEX, Base, TimestampMixin, pg_enum, uuid_pk

if TYPE_CHECKING:
    from app.modules.catalog.models import WebsiteProduct


class WebsiteStatus(enum.StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


class DomainStatus(enum.StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"


WEBSITE_KEY_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$"


class Website(TimestampMixin, Base):
    """A tenant. `key` is the immutable, human-readable identifier used in public URLs."""

    __tablename__ = "websites"
    __table_args__ = (
        CheckConstraint(f"key ~ '{WEBSITE_KEY_PATTERN}'", name="key_format"),
        CheckConstraint(
            "char_length(country_code) = 2 AND country_code = upper(country_code)",
            name="country_code_format",
        ),
        CheckConstraint(f"default_locale ~ '{LOCALE_CHECK_REGEX}'", name="default_locale_format"),
        CheckConstraint("cardinality(locales) > 0", name="locales_not_empty"),
        CheckConstraint("default_locale = ANY(locales)", name="default_locale_in_locales"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    country_code: Mapped[str] = mapped_column(String(2))
    default_locale: Mapped[str] = mapped_column(String(10))
    locales: Mapped[list[str]] = mapped_column(ARRAY(String(10)))
    status: Mapped[WebsiteStatus] = mapped_column(
        pg_enum(WebsiteStatus, "website_status"), server_default=WebsiteStatus.DRAFT.value
    )
    auto_publish: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Bumped on every change affecting the site; drives ETags and "unpublished changes".
    content_version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    published_version: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    domains: Mapped[list["WebsiteDomain"]] = relationship(
        back_populates="website",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    theme: Mapped["WebsiteTheme | None"] = relationship(
        back_populates="website",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    product_links: Mapped[list["WebsiteProduct"]] = relationship(
        back_populates="website",
        lazy="raise",
        passive_deletes=True,
    )


class WebsiteDomain(TimestampMixin, Base):
    """Hostname → website mapping. Only `active` domains resolve publicly."""

    __tablename__ = "website_domains"
    __table_args__ = (
        CheckConstraint("hostname = lower(hostname)", name="hostname_lowercase"),
        CheckConstraint(
            "hostname !~ '[:/\\s]' AND right(hostname, 1) <> '.'", name="hostname_bare"
        ),
        # At most one primary domain per website.
        Index(
            "uq_website_domains_primary",
            "website_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    website_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True
    )
    hostname: Mapped[str] = mapped_column(String(253), unique=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    status: Mapped[DomainStatus] = mapped_column(
        pg_enum(DomainStatus, "domain_status"), server_default=DomainStatus.PENDING.value
    )
    redirect_to_primary: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    cloudflare_zone_id: Mapped[str | None] = mapped_column(String(64))
    # Random 32-hex token for the DNS TXT check (_showcase-verify.<hostname>).
    verification_token: Mapped[str] = mapped_column(
        String(64), server_default=text("replace((gen_random_uuid())::text, '-'::text, ''::text)")
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    website: Mapped[Website] = relationship(back_populates="domains", lazy="raise")


class WebsiteTheme(TimestampMixin, Base):
    """Branding for a website (1:1). `tokens` is validated by schemas.ThemeTokens."""

    __tablename__ = "website_themes"

    website_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), primary_key=True
    )
    tokens: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    logo_key: Mapped[str | None] = mapped_column(Text)
    logo_dark_key: Mapped[str | None] = mapped_column(Text)
    favicon_key: Mapped[str | None] = mapped_column(Text)
    og_default_image_key: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))

    website: Mapped[Website] = relationship(back_populates="theme", lazy="raise")
