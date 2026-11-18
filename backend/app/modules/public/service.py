"""Assembles public responses for one website.

Inputs are pure (site, locale, params) and outputs depend only on the database state at
`site.content_version`, so responses can be cached under that version without any
invalidation logic (HTTP ETags today, an in-process/Redis cache later if needed).
"""

import math
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import NotFound, ValidationFailed
from app.core.locale import InvalidLocale, fallback_chain, normalize_locale, pick_translation
from app.core.media import public_url
from app.modules.catalog.models import ProductImage
from app.modules.content.models import PageType, SeoMetadata, WebsiteContent
from app.modules.content.schemas import FeaturedProductsBlock, Link, content_blocks_adapter
from app.modules.public import schemas as s
from app.modules.public import urls
from app.modules.public.reads import ProductRow, SiteReads, TranslationRef
from app.modules.websites.models import WebsiteTheme
from app.modules.websites.resolution import SiteRef
from app.modules.websites.schemas import ThemeTokens

DEFAULT_ROBOTS = "index,follow"
SCHEMA_ORG = "https://schema.org"

SeoIndex = Mapping[tuple[PageType, str, uuid.UUID | None], SeoMetadata]


def resolve_locale(site: SiteRef, raw: str | None) -> str:
    """Requested locale (normalized) if the site offers it; the default when omitted."""
    if raw is None or not raw.strip():
        return site.default_locale
    try:
        locale = normalize_locale(raw)
    except InvalidLocale as exc:
        raise ValidationFailed(str(exc), code="invalid_locale") from exc
    if locale not in site.locales:
        raise NotFound(
            f"Website {site.key!r} is not available in locale {locale!r}",
            code="locale_not_supported",
            extra={"locales": list(site.locales)},
        )
    return locale


def _latest(*values: datetime | None) -> datetime | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


class SiteReadService:
    def __init__(self, session: AsyncSession, settings: Settings, site: SiteRef) -> None:
        self.site = site
        self.settings = settings
        self.reads = SiteReads(session, site)

    def chain(self, locale: str) -> list[str]:
        return fallback_chain(locale, self.site.default_locale)

    # ==================================================================================
    # Small builders
    # ==================================================================================

    def _asset(self, key: str | None) -> s.Asset | None:
        url = public_url(self.settings, key)
        return s.Asset(key=key, url=url) if key and url else None

    def _image(self, img: ProductImage, chain: Sequence[str], fallback_alt: str) -> s.Image:
        picked = pick_translation(img.alt, chain)
        return s.Image(
            key=img.storage_key,
            url=public_url(self.settings, img.storage_key) or "",
            width=img.width,
            height=img.height,
            alt=picked[1] if picked else fallback_alt,
        )

    def _summary(
        self, row: ProductRow, locale: str, images: Mapping[uuid.UUID, ProductImage]
    ) -> s.ProductSummary:
        img = images.get(row.id)
        return s.ProductSummary(
            id=row.id,
            sku=row.sku,
            locale=row.locale,
            slug=row.slug,
            name=row.name,
            short_description=row.short_description,
            path=urls.product_path(self.site, locale, row.slug),
            is_featured=row.is_featured,
            image=self._image(img, self.chain(locale), row.name) if img else None,
        )

    async def _summaries(
        self, locale: str, **kwargs: Any
    ) -> tuple[list[s.ProductSummary], int, list[ProductRow], dict[uuid.UUID, ProductImage]]:
        rows, total = await self.reads.products(self.chain(locale), **kwargs)
        images = await self.reads.primary_images([r.id for r in rows])
        return [self._summary(r, locale, images) for r in rows], total, rows, images

    @staticmethod
    def _pick_content(
        contents: Mapping[tuple[PageType, str], WebsiteContent],
        page: PageType,
        chain: Sequence[str],
    ) -> WebsiteContent | None:
        for locale in chain:
            if (c := contents.get((page, locale))) is not None:
                return c
        return None

    def _page_content(self, content: WebsiteContent | None, locale: str) -> s.PageContent | None:
        if content is None:
            return None
        blocks = content_blocks_adapter.validate_python(content.blocks)
        for block in blocks:
            for link in (getattr(block, "cta", None), getattr(block, "link", None)):
                if isinstance(link, Link):
                    link.href = urls.localize_href(self.site, locale, link.href)
        return s.PageContent(
            locale=content.locale, title=content.title, subtitle=content.subtitle, blocks=blocks
        )

    def _theme(self, theme: WebsiteTheme | None) -> s.Theme | None:
        if theme is None:
            return None
        return s.Theme(
            tokens=ThemeTokens.model_validate(theme.tokens),
            version=theme.version,
            logo=self._asset(theme.logo_key),
            logo_dark=self._asset(theme.logo_dark_key),
            favicon=self._asset(theme.favicon_key),
            og_default_image=self._asset(theme.og_default_image_key),
        )

    def _site_alternates(self, path_for: Callable[[str], str]) -> list[s.Alternate]:
        return self._alternates({loc: path_for(loc) for loc in self.site.locales})

    def _alternates(self, paths: Mapping[str, str]) -> list[s.Alternate]:
        """hreflang links (+ x-default → default locale) for locales that have the page."""
        if len(paths) < 2:
            return []
        links = [
            s.Alternate(hreflang=urls.hreflang(self.site, loc), href=urls.absolute(self.site, p))
            for loc in self.site.locales
            if (p := paths.get(loc)) is not None
        ]
        if (default := paths.get(self.site.default_locale)) is not None:
            links.append(s.Alternate(hreflang="x-default", href=urls.absolute(self.site, default)))
        return links

    # ==================================================================================
    # SEO
    # ==================================================================================

    def _seo(
        self,
        *,
        page: s.SeoPage,
        locale: str,
        path: str,
        canonical_path: str,
        title: str,
        description: str | None,
        override: SeoMetadata | None,
        og_type: str,
        image_key: str | None,
        alternates: list[s.Alternate],
        json_ld: dict[str, Any],
    ) -> s.Seo:
        o = override
        title = (o.title if o and o.title else None) or title
        description = (o.description if o and o.description else None) or description
        canonical = (o.canonical_url if o else None) or urls.absolute(self.site, canonical_path)
        image = public_url(self.settings, (o.og_image_key if o else None) or image_key)
        if o and o.structured_data:
            ld = [o.structured_data]
        else:
            ld = [{"@context": SCHEMA_ORG, **json_ld, "url": canonical}]
            if image and og_type == "product":
                ld[0]["image"] = [image]
        return s.Seo(
            page=page,
            locale=locale,
            path=path,
            title=title,
            description=description,
            canonical=canonical,
            robots=(o.robots if o and o.robots else None) or DEFAULT_ROBOTS,
            og=s.OpenGraph(
                title=(o.og_title if o and o.og_title else None) or title,
                description=(o.og_description if o and o.og_description else None) or description,
                image=image,
                type="product" if og_type == "product" else "website",
                url=canonical,
                locale=urls.og_locale(self.site, locale),
                site_name=self.site.name,
            ),
            alternates=alternates,
            json_ld=ld,
        )

    def _page_seo(
        self,
        page: PageType,
        locale: str,
        content: WebsiteContent | None,
        seo_rows: SeoIndex,
        theme: WebsiteTheme | None,
    ) -> s.Seo:
        content_locale = content.locale if content else locale
        path_for = urls.home_path if page is PageType.HOME else urls.products_path
        path = path_for(self.site, locale)
        if page is PageType.HOME:
            title = f"{content.title} | {self.site.name}" if content else self.site.name
            ld: dict[str, Any] = {"@type": "WebSite", "name": self.site.name}
        else:
            title = f"{content.title if content else 'Products'} | {self.site.name}"
            ld = {"@type": "CollectionPage", "name": title}
        return self._seo(
            page=page.value,
            locale=locale,
            path=path,
            canonical_path=path,
            title=title,
            description=content.subtitle if content else None,
            override=seo_rows.get((page, content_locale, None)),
            og_type="website",
            image_key=theme.og_default_image_key if theme else None,
            alternates=self._site_alternates(lambda loc: path_for(self.site, loc)),
            json_ld={**ld, "inLanguage": content_locale},
        )

    def _product_seo(
        self,
        locale: str,
        row: ProductRow,
        seo_rows: SeoIndex,
        image: ProductImage | None,
        theme: WebsiteTheme | None,
        translations: Mapping[str, TranslationRef],
    ) -> s.Seo:
        return self._seo(
            page="product",
            locale=locale,
            path=urls.product_path(self.site, locale, row.slug),
            # Shown in a fallback locale → canonical is the page in its own language.
            canonical_path=urls.product_path(self.site, row.locale, row.slug),
            title=f"{row.name} | {self.site.name}",
            description=row.short_description,
            override=seo_rows.get((PageType.PRODUCT, row.locale, row.id)),
            og_type="product",
            image_key=(image.storage_key if image else None)
            or (theme.og_default_image_key if theme else None),
            alternates=self._alternates(
                {
                    loc: urls.product_path(self.site, loc, ref.slug)
                    for loc, ref in translations.items()
                }
            ),
            json_ld={
                "@type": "Product",
                "name": row.name,
                "sku": row.sku,
                "description": row.short_description,
                "inLanguage": row.locale,
            },
        )

    # ==================================================================================
    # Endpoints
    # ==================================================================================

    async def config(self, locale: str) -> s.SiteConfig:
        theme = await self.reads.theme()
        contents = await self.reads.content([PageType.PRODUCTS], self.chain(locale))
        return self._config(locale, theme, contents)

    def _config(
        self,
        locale: str,
        theme: WebsiteTheme | None,
        contents: Mapping[tuple[PageType, str], WebsiteContent],
    ) -> s.SiteConfig:
        site = self.site
        products = self._pick_content(contents, PageType.PRODUCTS, self.chain(locale))
        return s.SiteConfig(
            site=s.SiteInfo(
                key=site.key,
                name=site.name,
                country_code=site.country_code,
                default_locale=site.default_locale,
                locales=list(site.locales),
                primary_host=site.primary_host,
                base_url=site.base_url,
            ),
            locale=locale,
            theme=self._theme(theme),
            nav=[
                s.NavItem(page="home", label=site.name, path=urls.home_path(site, locale)),
                s.NavItem(
                    page="products",
                    label=products.title if products else "Products",
                    path=urls.products_path(site, locale),
                ),
            ],
            locale_links=[
                s.LocaleLink(locale=loc, path=urls.home_path(site, loc)) for loc in site.locales
            ],
            media=s.MediaSettings(
                base_url=self.settings.storage_public_base_url,
                transform_provider=self.settings.image_transform_provider,
            ),
            content_version=site.content_version,
        )

    async def home(self, locale: str) -> s.HomePage:
        contents = await self.reads.content([PageType.HOME], self.chain(locale))
        return await self._home(locale, contents)

    async def _home(
        self,
        locale: str,
        contents: Mapping[tuple[PageType, str], WebsiteContent],
        all_products: tuple[list[s.ProductSummary], list[ProductRow]] | None = None,
    ) -> s.HomePage:
        content = self._page_content(
            self._pick_content(contents, PageType.HOME, self.chain(locale)), locale
        )
        blocks = content.blocks if content else []
        limit = max((b.limit for b in blocks if isinstance(b, FeaturedProductsBlock)), default=0)
        featured: list[s.ProductSummary] = []
        if limit and all_products is not None:
            featured = [p for p in all_products[0] if p.is_featured][:limit]
        elif limit:
            featured, *_ = await self._summaries(locale, featured_only=True, limit=limit)
        return s.HomePage(
            locale=content.locale if content else locale,
            title=content.title if content else self.site.name,
            subtitle=content.subtitle if content else None,
            blocks=blocks,
            featured_products=featured,
        )

    async def products(self, locale: str, page: int, page_size: int) -> s.ProductList:
        contents = await self.reads.content([PageType.PRODUCTS], self.chain(locale))
        items, total, *_ = await self._summaries(
            locale, limit=page_size, offset=(page - 1) * page_size
        )
        return s.ProductList(
            content=self._page_content(
                self._pick_content(contents, PageType.PRODUCTS, self.chain(locale)), locale
            ),
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=math.ceil(total / page_size),
        )

    async def product(self, locale: str, slug: str) -> s.ProductDetail:
        found = await self.reads.product_by_slug(slug, self.chain(locale))
        if found is None:
            raise NotFound(f"Product {slug!r} not found", code="product_not_found")
        product, row = found
        translation = await self.reads.translation(product.id, row.locale)
        images = await self.reads.images(product.id)
        index = await self.reads.translation_index([product.id])
        summary = self._summary(row, locale, {product.id: images[0]} if images else {})
        chain = self.chain(locale)
        return s.ProductDetail(
            **summary.model_dump(),
            description=translation.description,
            attributes=product.attributes,
            translated_attributes=translation.attributes,
            images=[self._image(img, chain, row.name) for img in images],
            canonical_path=urls.product_path(self.site, row.locale, row.slug),
            alternates=self._alternates(
                {
                    loc: urls.product_path(self.site, loc, ref.slug)
                    for loc, ref in index.get(product.id, {}).items()
                }
            ),
        )

    async def seo(self, locale: str, page: s.SeoPage, slug: str | None) -> s.Seo:
        chain = self.chain(locale)
        theme = await self.reads.theme()
        if page == "product":
            if not slug:
                raise ValidationFailed("`slug` is required for page=product", code="slug_required")
            found = await self.reads.product_by_slug(slug, chain)
            if found is None:
                raise NotFound(f"Product {slug!r} not found", code="product_not_found")
            _, row = found
            seo_rows = await self.reads.seo_rows(
                [row.locale], pages=[PageType.PRODUCT], product_ids=[row.id]
            )
            images = await self.reads.primary_images([row.id])
            index = await self.reads.translation_index([row.id])
            return self._product_seo(
                locale, row, seo_rows, images.get(row.id), theme, index.get(row.id, {})
            )
        page_type = PageType(page)
        contents = await self.reads.content([page_type], chain)
        seo_rows = await self.reads.seo_rows(chain, pages=[page_type])
        return self._page_seo(
            page_type, locale, self._pick_content(contents, page_type, chain), seo_rows, theme
        )

    async def bundle(self, locale: str) -> s.Bundle:
        """Everything needed to statically build one locale of a site, in 7 queries."""
        chain = self.chain(locale)
        theme = await self.reads.theme()
        contents = await self.reads.content([PageType.HOME, PageType.PRODUCTS], chain)
        products, _, rows, images = await self._summaries(locale)
        seo_rows = await self.reads.seo_rows(chain)
        index = await self.reads.translation_index()

        product_seo = {
            row.slug: self._product_seo(
                locale, row, seo_rows, images.get(row.id), theme, index.get(row.id, {})
            )
            for row in rows
        }
        home_content = self._pick_content(contents, PageType.HOME, chain)
        products_content = self._pick_content(contents, PageType.PRODUCTS, chain)
        return s.Bundle(
            config=self._config(locale, theme, contents),
            home=await self._home(locale, contents, (products, rows)),
            products_page=self._page_content(products_content, locale),
            products=products,
            slugs=[p.slug for p in products],
            seo=s.BundleSeo(
                home=self._page_seo(PageType.HOME, locale, home_content, seo_rows, theme),
                products=self._page_seo(
                    PageType.PRODUCTS, locale, products_content, seo_rows, theme
                ),
                product=product_seo,
            ),
        )

    async def sitemap(self, locale: str | None) -> s.Sitemap:
        """Indexable URLs. A product is listed per locale it is actually translated into
        (pages shown in a fallback language canonicalize to the original instead)."""
        site = self.site
        locales = [locale] if locale else list(site.locales)
        contents = await self.reads.content([PageType.HOME, PageType.PRODUCTS], site.locales)
        index = await self.reads.translation_index()
        latest_product = _latest(*(ref.updated_at for t in index.values() for ref in t.values()))

        home_alts = self._site_alternates(lambda loc: urls.home_path(site, loc))
        list_alts = self._site_alternates(lambda loc: urls.products_path(site, loc))
        entries: list[s.SitemapEntry] = []

        def add(loc: str, path: str, lastmod: datetime | None, alts: list[s.Alternate]) -> None:
            entries.append(
                s.SitemapEntry(
                    locale=loc,
                    path=path,
                    loc=urls.absolute(site, path),
                    lastmod=lastmod,
                    alternates=alts,
                )
            )

        for loc in locales:
            chain = self.chain(loc)
            home = self._pick_content(contents, PageType.HOME, chain)
            plist = self._pick_content(contents, PageType.PRODUCTS, chain)
            add(loc, urls.home_path(site, loc), home.updated_at if home else None, home_alts)
            add(
                loc,
                urls.products_path(site, loc),
                _latest(plist.updated_at if plist else None, latest_product),
                list_alts,
            )
            translated = sorted((t for t in index.values() if loc in t), key=lambda t: t[loc].slug)
            for translations in translated:
                ref = translations[loc]
                paths = {lc: urls.product_path(site, lc, r.slug) for lc, r in translations.items()}
                add(loc, paths[loc], ref.updated_at, self._alternates(paths))
        return s.Sitemap(entries=entries)
