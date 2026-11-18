"""Public URL scheme (single source of truth for paths, canonicals and hreflang).

    default locale   /            /products            /products/{slug}
    other locales    /{locale}    /{locale}/products   /{locale}/products/{slug}

The Next.js middleware applies the same rule when rewriting to
`/_sites/{siteKey}/{locale}/...`.
"""

from app.modules.websites.resolution import SiteRef


def locale_prefix(site: SiteRef, locale: str) -> str:
    return "" if locale == site.default_locale else f"/{locale}"


def home_path(site: SiteRef, locale: str) -> str:
    return locale_prefix(site, locale) or "/"


def products_path(site: SiteRef, locale: str) -> str:
    return f"{locale_prefix(site, locale)}/products"


def product_path(site: SiteRef, locale: str, slug: str) -> str:
    return f"{locale_prefix(site, locale)}/products/{slug}"


def localize_href(site: SiteRef, locale: str, href: str) -> str:
    """Internal links in content blocks are written locale-neutral (`/products`); prefix
    them for the page's locale. External, protocol-relative and anchor links pass through."""
    prefix = locale_prefix(site, locale)
    if not prefix or not href.startswith("/") or href.startswith("//"):
        return href
    if href == prefix or href.startswith((f"{prefix}/", f"{prefix}?", f"{prefix}#")):
        return href
    return prefix if href == "/" else f"{prefix}{href}"


def absolute(site: SiteRef, path: str) -> str:
    """Absolute URL on the primary domain; the bare path if the site has none yet."""
    return f"{site.base_url}{path}" if site.base_url else path


def hreflang(site: SiteRef, locale: str) -> str:
    """Language + the site's country (`de` on a DE site → `de-DE`), so sites sharing a
    language in different countries (DE/AT/CH) don't compete in search results."""
    return locale if "-" in locale else f"{locale}-{site.country_code}"


def og_locale(site: SiteRef, locale: str) -> str:
    return hreflang(site, locale).replace("-", "_")
