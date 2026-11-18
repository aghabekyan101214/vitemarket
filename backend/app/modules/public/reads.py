"""Set-based read queries for one website. Every query is scoped by `site.id`.

No query here runs per product: listings use a LATERAL join to pick each product's best
translation in one statement, and images are fetched for all listed products at once.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, case, func, select, true
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import (
    Product,
    ProductImage,
    ProductStatus,
    ProductTranslation,
    WebsiteProduct,
)
from app.modules.content.models import PageType, SeoMetadata, WebsiteContent
from app.modules.websites.models import WebsiteTheme
from app.modules.websites.resolution import SiteRef

PT = ProductTranslation
WP = WebsiteProduct


@dataclass(frozen=True, slots=True)
class ProductRow:
    """A visible product with the translation picked by the locale fallback chain."""

    id: uuid.UUID
    sku: str
    is_featured: bool
    locale: str
    name: str
    slug: str
    short_description: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TranslationRef:
    slug: str
    updated_at: datetime  # max of product, assignment and translation timestamps


def _rank(chain: Sequence[str]) -> ColumnElement[int]:
    """0 for the requested locale, 1 for the next fallback, …"""
    return case({loc: i for i, loc in enumerate(chain)}, value=PT.locale, else_=len(chain))


class SiteReads:
    def __init__(self, session: AsyncSession, site: SiteRef) -> None:
        self.session = session
        self.site = site

    # --- visibility -------------------------------------------------------------------

    def _visible(self) -> list[ColumnElement[bool]]:
        return [
            WP.website_id == self.site.id,
            WP.is_visible.is_(True),
            Product.status == ProductStatus.ACTIVE,
        ]

    # --- theme & content --------------------------------------------------------------

    async def theme(self) -> WebsiteTheme | None:
        return await self.session.get(WebsiteTheme, self.site.id)

    async def content(
        self, pages: Collection[PageType], locales: Collection[str]
    ) -> dict[tuple[PageType, str], WebsiteContent]:
        rows = await self.session.scalars(
            select(WebsiteContent).where(
                WebsiteContent.website_id == self.site.id,
                WebsiteContent.page.in_(pages),
                WebsiteContent.locale.in_(locales),
            )
        )
        return {(c.page, c.locale): c for c in rows}

    async def seo_rows(
        self,
        locales: Collection[str],
        *,
        pages: Collection[PageType] | None = None,
        product_ids: Collection[uuid.UUID] | None = None,
    ) -> dict[tuple[PageType, str, uuid.UUID | None], SeoMetadata]:
        stmt = select(SeoMetadata).where(
            SeoMetadata.website_id == self.site.id, SeoMetadata.locale.in_(locales)
        )
        if pages is not None:
            stmt = stmt.where(SeoMetadata.page.in_(pages))
        if product_ids is not None:
            stmt = stmt.where(
                SeoMetadata.product_id.is_(None) | SeoMetadata.product_id.in_(product_ids)
            )
        rows = await self.session.scalars(stmt)
        return {(r.page, r.locale, r.product_id): r for r in rows}

    # --- products ---------------------------------------------------------------------

    def _product_rows(self, chain: Sequence[str]) -> Select[Any]:
        best = (
            select(PT.locale, PT.name, PT.slug, PT.short_description, PT.updated_at)
            .where(PT.product_id == WP.product_id, PT.locale.in_(chain))
            .order_by(_rank(chain))
            .limit(1)
            .lateral("tr")
        )
        return (
            select(
                Product.id,
                Product.sku,
                WP.is_featured,
                best.c.locale,
                best.c.name,
                best.c.slug,
                best.c.short_description,
                func.greatest(Product.updated_at, WP.updated_at, best.c.updated_at).label(
                    "updated_at"
                ),
            )
            .select_from(WP)
            .join(Product, Product.id == WP.product_id)
            .join(best, true())
            .where(*self._visible())
        )

    @staticmethod
    def _to_row(r: Any) -> ProductRow:
        return ProductRow(
            id=r.id,
            sku=r.sku,
            is_featured=r.is_featured,
            locale=r.locale,
            name=r.name,
            slug=r.slug,
            short_description=r.short_description,
            updated_at=r.updated_at,
        )

    async def products(
        self,
        chain: Sequence[str],
        *,
        featured_only: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ProductRow], int]:
        """Visible products in display order + total count (one query).

        Products without a translation in the chain are omitted (never shown in another
        language).
        """
        stmt = self._product_rows(chain).add_columns(func.count().over().label("total"))
        if featured_only:
            stmt = stmt.where(WP.is_featured.is_(True))
        stmt = stmt.order_by(WP.position, WP.product_id).offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await self.session.execute(stmt)).all()
        if rows:
            return [self._to_row(r) for r in rows], rows[0].total
        if offset == 0:
            return [], 0
        # Page past the end: the window count is unavailable, count separately.
        total = await self.session.scalar(
            select(func.count()).select_from(self._product_rows(chain).subquery())
        )
        return [], total or 0

    async def product_by_slug(
        self, slug: str, chain: Sequence[str]
    ) -> tuple[Product, ProductRow] | None:
        """Visible product whose slug matches in any chain locale.

        The returned row carries the *best* translation for the chain, which may have a
        different slug than the one requested (e.g. the default-locale slug was used).
        """
        product_id = await self.session.scalar(
            select(PT.product_id)
            .join(WP, WP.product_id == PT.product_id)
            .join(Product, Product.id == PT.product_id)
            .where(PT.slug == slug, PT.locale.in_(chain), *self._visible())
            .order_by(_rank(chain))
            .limit(1)
        )
        if product_id is None:
            return None
        row = (
            await self.session.execute(self._product_rows(chain).where(Product.id == product_id))
        ).one()
        product = await self.session.get_one(Product, product_id)
        return product, self._to_row(row)

    async def translation(self, product_id: uuid.UUID, locale: str) -> ProductTranslation:
        return await self.session.get_one(PT, (product_id, locale))

    async def translation_index(
        self, product_ids: Collection[uuid.UUID] | None = None
    ) -> dict[uuid.UUID, dict[str, TranslationRef]]:
        """Slugs of visible products in every site locale (for hreflang and sitemaps)."""
        stmt = (
            select(
                PT.product_id,
                PT.locale,
                PT.slug,
                func.greatest(Product.updated_at, WP.updated_at, PT.updated_at).label("ts"),
            )
            .select_from(PT)
            .join(WP, WP.product_id == PT.product_id)
            .join(Product, Product.id == PT.product_id)
            .where(*self._visible(), PT.locale.in_(self.site.locales))
        )
        if product_ids is not None:
            stmt = stmt.where(PT.product_id.in_(product_ids))
        index: dict[uuid.UUID, dict[str, TranslationRef]] = {}
        for pid, locale, slug, ts in await self.session.execute(stmt):
            index.setdefault(pid, {})[locale] = TranslationRef(slug=slug, updated_at=ts)
        return index

    # --- images -----------------------------------------------------------------------

    async def primary_images(
        self, product_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, ProductImage]:
        if not product_ids:
            return {}
        rows = await self.session.scalars(
            select(ProductImage)
            .where(ProductImage.product_id.in_(product_ids))
            .ext(distinct_on(ProductImage.product_id))
            .order_by(ProductImage.product_id, ProductImage.position, ProductImage.id)
        )
        return {img.product_id: img for img in rows}

    async def images(self, product_id: uuid.UUID) -> list[ProductImage]:
        rows = await self.session.scalars(
            select(ProductImage)
            .where(ProductImage.product_id == product_id)
            .order_by(ProductImage.position, ProductImage.id)
        )
        return list(rows)
