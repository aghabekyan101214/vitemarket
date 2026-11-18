"""Public, read-only API (`/public/v1`), consumed by the Next.js frontends at build and
revalidation time. Visitors never call it directly.

Every site-scoped response carries a weak ETag derived from the site's `content_version`:
a matching `If-None-Match` is answered with 304 after a single indexed lookup.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SettingsDep
from app.core.db import DatabaseDep
from app.core.http_cache import (
    CONTENT_CACHE,
    ROUTING_CACHE,
    body_cached_response,
    json_response,
    not_modified,
    version_etag,
)
from app.modules.catalog.models import SLUG_PATTERN
from app.modules.public import schemas as s
from app.modules.public.service import SiteReadService, resolve_locale
from app.modules.websites.models import WEBSITE_KEY_PATTERN
from app.modules.websites.resolution import (
    SiteRef,
    get_active_site,
    list_active_sites,
    resolve_host,
)

router = APIRouter(prefix="/public/v1", tags=["public"])

MAX_PAGE_SIZE = 100

_NOT_MODIFIED: dict[int | str, dict[str, Any]] = {
    304: {"description": "Not modified (the `If-None-Match` ETag still matches)"}
}
_SITE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_NOT_MODIFIED,
    404: {"description": "Unknown or inactive website, or locale not offered by the site"},
}


# --- dependencies ---------------------------------------------------------------------


async def get_public_session(db: DatabaseDep, settings: SettingsDep) -> AsyncIterator[AsyncSession]:
    """Read-only REPEATABLE READ transaction: all queries of one response see the same
    snapshot (consistent with the content_version in the ETag), writes are impossible, and
    slow queries are cut off early."""
    async with db.sessionmaker() as session:
        await session.connection(
            execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
        )
        if settings.public_statement_timeout_ms:
            await session.execute(
                text("SELECT set_config('statement_timeout', :ms, true)"),
                {"ms": str(settings.public_statement_timeout_ms)},
            )
        yield session


PublicSession = Annotated[AsyncSession, Depends(get_public_session)]


async def get_site(
    site: Annotated[
        str, Path(pattern=WEBSITE_KEY_PATTERN, max_length=50, description="Website key")
    ],
    session: PublicSession,
) -> SiteRef:
    return await get_active_site(session, site)


SiteDep = Annotated[SiteRef, Depends(get_site)]


def get_service(site: SiteDep, session: PublicSession, settings: SettingsDep) -> SiteReadService:
    return SiteReadService(session, settings, site)


ServiceDep = Annotated[SiteReadService, Depends(get_service)]

LocaleQuery = Annotated[
    str | None,
    Query(
        max_length=10,
        description="Locale (e.g. `de`, `en`). Defaults to the site's default locale; "
        "content missing in this locale falls back to the default locale.",
    ),
]


async def _versioned(
    request: Request,
    site: SiteRef,
    endpoint: str,
    params: dict[str, object],
    build: Callable[[], Awaitable[BaseModel]],
) -> Response:
    etag = version_etag(str(site.id), site.content_version, endpoint, params)
    if (cached := not_modified(request, etag, CONTENT_CACHE)) is not None:
        return cached
    return json_response(await build(), etag=etag, policy=CONTENT_CACHE)


# --- routing --------------------------------------------------------------------------


@router.get(
    "/resolve",
    response_model=s.ResolvedHost,
    summary="Resolve a hostname to its website",
    responses={**_NOT_MODIFIED, 404: {"description": "No active website for this host"}},
)
async def resolve(
    request: Request,
    session: PublicSession,
    host: Annotated[str, Query(min_length=1, max_length=260, description="Host header value")],
) -> Response:
    """Fallback/tooling endpoint. The frontend middleware normally uses the `/sites` map."""
    res = await resolve_host(session, host)
    body = s.ResolvedHost(
        website_key=res.site.key,
        website_id=res.site.id,
        hostname=res.hostname,
        locale=res.site.default_locale,
        locales=list(res.site.locales),
        primary_host=res.site.primary_host,
        redirect=res.redirect_to,
        content_version=res.site.content_version,
    )
    return body_cached_response(request, body, ROUTING_CACHE)


@router.get(
    "/sites",
    response_model=s.SiteList,
    summary="All active websites with their hosts",
    responses=_NOT_MODIFIED,
)
async def sites(request: Request, session: PublicSession) -> Response:
    """Host → site map for the frontend middleware and `generateStaticParams`."""
    items = [
        s.SiteListItem(
            key=entry.site.key,
            id=entry.site.id,
            name=entry.site.name,
            default_locale=entry.site.default_locale,
            locales=list(entry.site.locales),
            primary_host=entry.site.primary_host,
            content_version=entry.site.content_version,
            hosts=[
                s.SiteHost(
                    hostname=d.hostname,
                    is_primary=d.is_primary,
                    redirect_to_primary=d.redirect_to_primary,
                )
                for d in entry.hosts
            ],
        )
        for entry in await list_active_sites(session)
    ]
    return body_cached_response(request, s.SiteList(sites=items), ROUTING_CACHE)


# --- site content ---------------------------------------------------------------------


@router.get(
    "/sites/{site}/config",
    response_model=s.SiteConfig,
    summary="Site settings, theme, navigation",
    responses=_SITE_RESPONSES,
)
async def config(request: Request, svc: ServiceDep, locale: LocaleQuery = None) -> Response:
    loc = resolve_locale(svc.site, locale)
    return await _versioned(request, svc.site, "config", {"locale": loc}, lambda: svc.config(loc))


@router.get(
    "/sites/{site}/home",
    response_model=s.HomePage,
    summary="Home page content and featured products",
    responses=_SITE_RESPONSES,
)
async def home(request: Request, svc: ServiceDep, locale: LocaleQuery = None) -> Response:
    loc = resolve_locale(svc.site, locale)
    return await _versioned(request, svc.site, "home", {"locale": loc}, lambda: svc.home(loc))


@router.get(
    "/sites/{site}/products",
    response_model=s.ProductList,
    summary="Visible products (paginated)",
    responses=_SITE_RESPONSES,
)
async def products(
    request: Request,
    svc: ServiceDep,
    locale: LocaleQuery = None,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 24,
) -> Response:
    loc = resolve_locale(svc.site, locale)
    return await _versioned(
        request,
        svc.site,
        "products",
        {"locale": loc, "page": page, "page_size": page_size},
        lambda: svc.products(loc, page, page_size),
    )


@router.get(
    "/sites/{site}/products/{slug}",
    response_model=s.ProductDetail,
    summary="Product detail",
    responses={**_SITE_RESPONSES, 404: {"description": "Unknown website, locale or product"}},
)
async def product(
    request: Request,
    svc: ServiceDep,
    slug: Annotated[str, Path(pattern=SLUG_PATTERN, max_length=200)],
    locale: LocaleQuery = None,
) -> Response:
    """Accepts the slug of any locale in the fallback chain. When the returned `path`
    differs from the requested one, the frontend should redirect to it."""
    loc = resolve_locale(svc.site, locale)
    return await _versioned(
        request,
        svc.site,
        "product",
        {"locale": loc, "slug": slug},
        lambda: svc.product(loc, slug),
    )


@router.get(
    "/sites/{site}/seo",
    response_model=s.Seo,
    summary="Resolved SEO metadata for a page",
    responses=_SITE_RESPONSES,
)
async def seo(
    request: Request,
    svc: ServiceDep,
    page: Annotated[s.SeoPage, Query()],
    locale: LocaleQuery = None,
    slug: Annotated[str | None, Query(pattern=SLUG_PATTERN, max_length=200)] = None,
) -> Response:
    """Overrides from `seo_metadata`, falling back to page content and site defaults."""
    loc = resolve_locale(svc.site, locale)
    return await _versioned(
        request,
        svc.site,
        "seo",
        {"locale": loc, "page": page, "slug": slug},
        lambda: svc.seo(loc, page, slug),
    )


@router.get(
    "/sites/{site}/bundle",
    response_model=s.Bundle,
    summary="Everything needed to build one locale of a site",
    responses=_SITE_RESPONSES,
)
async def bundle(request: Request, svc: ServiceDep, locale: LocaleQuery = None) -> Response:
    """Config, home, products page, all visible product summaries and SEO for every page,
    built from a fixed number of queries regardless of catalog size."""
    loc = resolve_locale(svc.site, locale)
    return await _versioned(request, svc.site, "bundle", {"locale": loc}, lambda: svc.bundle(loc))


@router.get(
    "/sites/{site}/sitemap",
    response_model=s.Sitemap,
    summary="Sitemap entries with hreflang alternates",
    responses=_SITE_RESPONSES,
)
async def sitemap(
    request: Request,
    svc: ServiceDep,
    locale: Annotated[
        str | None, Query(max_length=10, description="Limit to one locale; default: all")
    ] = None,
) -> Response:
    loc = resolve_locale(svc.site, locale) if locale else None
    return await _versioned(request, svc.site, "sitemap", {"locale": loc}, lambda: svc.sitemap(loc))
