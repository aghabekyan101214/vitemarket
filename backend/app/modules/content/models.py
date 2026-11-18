import enum
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import LOCALE_CHECK_REGEX, Base, TimestampMixin, pg_enum, uuid_pk


class PageType(enum.StrEnum):
    HOME = "home"
    PRODUCTS = "products"
    PRODUCT = "product"


class WebsiteContent(TimestampMixin, Base):
    """Per-website, per-locale page content. `blocks` is validated by schemas.ContentBlock."""

    __tablename__ = "website_content"
    __table_args__ = (
        UniqueConstraint("website_id", "page", "locale"),
        CheckConstraint("page IN ('home', 'products')", name="page_allowed"),
        CheckConstraint(f"locale ~ '{LOCALE_CHECK_REGEX}'", name="locale_format"),
        CheckConstraint("jsonb_typeof(blocks) = 'array'", name="blocks_is_array"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    website_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("websites.id", ondelete="CASCADE"))
    page: Mapped[PageType] = mapped_column(pg_enum(PageType, "page_type"))
    locale: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(300))
    subtitle: Mapped[str | None] = mapped_column(Text)
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))


class SeoMetadata(TimestampMixin, Base):
    """SEO overrides per page. Missing fields are derived from content at read time."""

    __tablename__ = "seo_metadata"
    __table_args__ = (
        # One row per (site, locale, page[, product]); NULL product_id counts as equal.
        UniqueConstraint(
            "website_id", "locale", "page", "product_id", postgresql_nulls_not_distinct=True
        ),
        CheckConstraint(
            "(page = 'product') = (product_id IS NOT NULL)", name="product_only_for_product_page"
        ),
        CheckConstraint(f"locale ~ '{LOCALE_CHECK_REGEX}'", name="locale_format"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    website_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("websites.id", ondelete="CASCADE"))
    locale: Mapped[str] = mapped_column(String(10))
    page: Mapped[PageType] = mapped_column(pg_enum(PageType, "page_type"))
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(String(500))
    canonical_url: Mapped[str | None] = mapped_column(Text)
    og_title: Mapped[str | None] = mapped_column(String(300))
    og_description: Mapped[str | None] = mapped_column(String(500))
    og_image_key: Mapped[str | None] = mapped_column(Text)
    robots: Mapped[str] = mapped_column(String(100), server_default="index,follow")
    structured_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
