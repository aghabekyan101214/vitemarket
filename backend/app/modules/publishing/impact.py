"""Which cache tags / paths of a website a change affects (pure functions).

Tags match the Next.js `fetch(..., {next: {tags}})` contract (PLAN.md §7):

    sites                              host → site map
    site:{id}                          everything of a site (theme, settings, nav)
    site:{id}:home:{locale}            home page
    site:{id}:products:{locale}        product listing
    site:{id}:product:{product_id}     product page (all locales)

Paths are site-relative and used for CDN purges by URL.
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.modules.public import urls
from app.modules.websites.resolution import SiteRef

SITES_TAG = "sites"


@dataclass(slots=True)
class Change:
    tags: set[str] = field(default_factory=set)
    paths: set[str] = field(default_factory=set)
    full_site: bool = False

    def merge(self, other: "Change") -> "Change":
        self.tags |= other.tags
        self.paths |= other.paths
        self.full_site = self.full_site or other.full_site
        return self


def site_tag(website_id: uuid.UUID) -> str:
    return f"site:{website_id}"


def whole_site(site: SiteRef, *, routing: bool = False) -> Change:
    """Settings, theme, locales, nav: every page of the site. `routing` → host map too."""
    tags = {site_tag(site.id)}
    if routing:
        tags.add(SITES_TAG)
    return Change(tags=tags, full_site=True)


def home(site: SiteRef, locales: Iterable[str]) -> Change:
    locales = list(locales)
    return Change(
        tags={f"site:{site.id}:home:{loc}" for loc in locales},
        paths={urls.home_path(site, loc) for loc in locales},
    )


def listing(site: SiteRef, locales: Iterable[str]) -> Change:
    locales = list(locales)
    return Change(
        tags={f"site:{site.id}:products:{loc}" for loc in locales},
        paths={urls.products_path(site, loc) for loc in locales},
    )


def product(
    site: SiteRef,
    product_id: uuid.UUID,
    slugs: Mapping[str, str],
    *,
    featured: bool,
    in_listing: bool = True,
) -> Change:
    """A product page in every site locale (fallback locales use the default slug), plus
    the listing and, for featured products, the home page."""
    change = Change(tags={f"site:{site.id}:product:{product_id}"})
    for loc in site.locales:
        slug = slugs.get(loc) or slugs.get(site.default_locale)
        if slug:
            change.paths.add(urls.product_path(site, loc, slug))
    if in_listing:
        change.merge(listing(site, site.locales))
    if featured:
        change.merge(home(site, site.locales))
    return change


def content_page(site: SiteRef, page: str, locale: str) -> Change:
    if page == "home":
        return home(site, [locale])
    # The products page title is the nav label shown on every page.
    return whole_site(site)


def seo(
    site: SiteRef,
    page: str,
    locale: str,
    *,
    product_id: uuid.UUID | None = None,
    slugs: Mapping[str, str] | None = None,
) -> Change:
    if page == "home":
        return home(site, [locale])
    if page == "products":
        return listing(site, [locale])
    assert product_id is not None
    return product(site, product_id, slugs or {}, featured=False, in_listing=False)
