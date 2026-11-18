"""Response models of the public, read-only API (`/public/v1`).

Paths are site-relative (`/en/products/oak-lounge-chair`); absolute URLs (`canonical`,
`alternates[].href`, sitemap `loc`) use the site's active primary domain.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.content.schemas import ContentBlock
from app.modules.websites.schemas import ThemeTokens


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


# --- routing --------------------------------------------------------------------------


class ResolvedHost(_Out):
    website_key: str
    website_id: uuid.UUID
    hostname: str
    locale: str = Field(description="The website's default locale")
    locales: list[str]
    primary_host: str | None
    redirect: str | None = Field(
        description="Absolute origin to 301-redirect to when the host is a redirecting alias"
    )
    content_version: int


class SiteHost(_Out):
    hostname: str
    is_primary: bool
    redirect_to_primary: bool


class SiteListItem(_Out):
    key: str
    id: uuid.UUID
    name: str
    default_locale: str
    locales: list[str]
    primary_host: str | None
    content_version: int
    hosts: list[SiteHost]


class SiteList(_Out):
    sites: list[SiteListItem]


# --- shared ---------------------------------------------------------------------------


class Asset(_Out):
    key: str
    url: str


class Image(_Out):
    key: str
    url: str
    width: int
    height: int
    alt: str


class Alternate(_Out):
    hreflang: str
    href: str


class LocaleLink(_Out):
    locale: str
    path: str


# --- config ---------------------------------------------------------------------------


class SiteInfo(_Out):
    key: str
    name: str
    country_code: str
    default_locale: str
    locales: list[str]
    primary_host: str | None
    base_url: str | None


class Theme(_Out):
    tokens: ThemeTokens
    version: int
    logo: Asset | None
    logo_dark: Asset | None
    favicon: Asset | None
    og_default_image: Asset | None


class MediaSettings(_Out):
    base_url: str
    transform_provider: Literal["none", "cloudflare"]


class NavItem(_Out):
    page: Literal["home", "products"]
    label: str
    path: str


class SiteConfig(_Out):
    site: SiteInfo
    locale: str
    theme: Theme | None
    nav: list[NavItem]
    locale_links: list[LocaleLink] = Field(description="Language switcher: home path per locale")
    media: MediaSettings
    content_version: int


# --- pages & products -----------------------------------------------------------------


class PageContent(_Out):
    locale: str = Field(description="Locale the content is in (may be the default locale)")
    title: str
    subtitle: str | None
    blocks: list[ContentBlock]


class ProductSummary(_Out):
    id: uuid.UUID
    sku: str
    locale: str = Field(description="Locale of the translation shown (may be the default)")
    slug: str
    name: str
    short_description: str | None
    path: str
    is_featured: bool
    image: Image | None


class ProductDetail(ProductSummary):
    description: str | None
    attributes: dict[str, Any] = Field(description="Shared, language-neutral attributes")
    translated_attributes: dict[str, Any]
    images: list[Image]
    canonical_path: str = Field(
        description="Differs from `path` when the product is shown in a fallback locale"
    )
    alternates: list[Alternate]


class HomePage(PageContent):
    featured_products: list[ProductSummary]


class ProductList(_Out):
    content: PageContent | None
    items: list[ProductSummary]
    page: int
    page_size: int
    total: int
    total_pages: int


# --- SEO ------------------------------------------------------------------------------

SeoPage = Literal["home", "products", "product"]


class OpenGraph(_Out):
    title: str
    description: str | None
    image: str | None
    type: Literal["website", "product"]
    url: str
    locale: str
    site_name: str


class Seo(_Out):
    page: SeoPage
    locale: str
    path: str
    title: str
    description: str | None
    canonical: str
    robots: str
    og: OpenGraph
    alternates: list[Alternate]
    json_ld: list[dict[str, Any]]


# --- bundle & sitemap -----------------------------------------------------------------


class BundleSeo(_Out):
    home: Seo
    products: Seo
    product: dict[str, Seo] = Field(description="Keyed by product slug")


class Bundle(_Out):
    config: SiteConfig
    home: HomePage
    products_page: PageContent | None
    products: list[ProductSummary]
    slugs: list[str]
    seo: BundleSeo


class SitemapEntry(_Out):
    locale: str
    path: str
    loc: str
    lastmod: datetime | None
    alternates: list[Alternate]


class Sitemap(_Out):
    entries: list[SitemapEntry]
