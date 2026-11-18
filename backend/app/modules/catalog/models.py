import enum
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import LOCALE_CHECK_REGEX, Base, TimestampMixin, pg_enum, uuid_pk

if TYPE_CHECKING:
    from app.modules.websites.models import Website

SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"


class ProductStatus(enum.StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Product(TimestampMixin, Base):
    """Shared, language-neutral product data. Reused across websites."""

    __tablename__ = "products"

    id: Mapped[uuid.UUID] = uuid_pk()
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[ProductStatus] = mapped_column(
        pg_enum(ProductStatus, "product_status"), server_default=ProductStatus.ACTIVE.value
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    translations: Mapped[list["ProductTranslation"]] = relationship(
        back_populates="product",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    images: Mapped[list["ProductImage"]] = relationship(
        back_populates="product",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ProductImage.position",
    )
    website_links: Mapped[list["WebsiteProduct"]] = relationship(
        back_populates="product",
        lazy="raise",
        passive_deletes=True,
    )


class ProductTranslation(TimestampMixin, Base):
    __tablename__ = "product_translations"
    __table_args__ = (
        UniqueConstraint("locale", "slug"),
        CheckConstraint(f"locale ~ '{LOCALE_CHECK_REGEX}'", name="locale_format"),
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="slug_format"),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), primary_key=True
    )
    locale: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(200))
    short_description: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    product: Mapped[Product] = relationship(back_populates="translations", lazy="raise")


class WebsiteProduct(TimestampMixin, Base):
    """Assignment of a shared product to a website, with per-site ordering and visibility."""

    __tablename__ = "website_products"
    __table_args__ = (
        # Public listing: visible products of a site in display order.
        Index(
            "ix_website_products_listing",
            "website_id",
            "position",
            "product_id",
            postgresql_where=text("is_visible"),
        ),
    )

    website_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), primary_key=True
    )
    # RESTRICT: a product assigned to a site must be unassigned (or archived) first.
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    position: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    is_visible: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    is_featured: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    website: Mapped["Website"] = relationship(back_populates="product_links", lazy="raise")
    product: Mapped[Product] = relationship(back_populates="website_links", lazy="raise")


class ProductImage(TimestampMixin, Base):
    __tablename__ = "product_images"
    __table_args__ = (
        Index("ix_product_images_product_position", "product_id", "position"),
        CheckConstraint("width > 0 AND height > 0 AND size_bytes > 0", name="dimensions_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    storage_key: Mapped[str] = mapped_column(Text, unique=True)  # immutable object key
    content_type: Mapped[str] = mapped_column(String(50))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    alt: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    product: Mapped[Product] = relationship(back_populates="images", lazy="raise")
