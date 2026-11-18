"""Tenant resolution: hostname → website, website key → site reference.

Every public read starts here. The result (`SiteRef`) carries the website id that all
subsequent queries are parameterized with; client-supplied ids are never used.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Row, Select, and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import NotFound
from app.core.hostnames import InvalidHostname, normalize_hostname
from app.modules.websites.models import DomainStatus, Website, WebsiteDomain, WebsiteStatus


@dataclass(frozen=True, slots=True)
class SiteRef:
    id: uuid.UUID
    key: str
    name: str
    country_code: str
    default_locale: str
    locales: tuple[str, ...]
    content_version: int
    primary_host: str | None  # active primary domain, if any

    @classmethod
    def of(cls, website: Website, primary_host: str | None = None) -> "SiteRef":
        return cls(
            id=website.id,
            key=website.key,
            name=website.name,
            country_code=website.country_code,
            default_locale=website.default_locale,
            locales=tuple(website.locales),
            content_version=website.content_version,
            primary_host=primary_host,
        )

    @property
    def base_url(self) -> str | None:
        return f"https://{self.primary_host}" if self.primary_host else None


@dataclass(frozen=True, slots=True)
class HostResolution:
    site: SiteRef
    hostname: str
    is_primary: bool
    redirect_to: str | None  # absolute origin to 301 to (alias of a primary domain)


_Primary = aliased(WebsiteDomain, name="primary_domain")

_SITE_COLUMNS = (
    Website.id,
    Website.key,
    Website.name,
    Website.country_code,
    Website.default_locale,
    Website.locales,
    Website.content_version,
    _Primary.hostname.label("primary_host"),
)


def _with_primary_host[*Ts](stmt: Select[*Ts]) -> Select[*Ts]:
    return stmt.outerjoin(
        _Primary,
        and_(
            _Primary.website_id == Website.id,
            _Primary.is_primary,
            _Primary.status == DomainStatus.ACTIVE,
        ),
    )


def _site_ref(row: Row[*tuple[Any, ...]]) -> SiteRef:
    return SiteRef(
        id=row.id,
        key=row.key,
        name=row.name,
        country_code=row.country_code,
        default_locale=row.default_locale,
        locales=tuple(row.locales),
        content_version=row.content_version,
        primary_host=row.primary_host,
    )


async def get_active_site(session: AsyncSession, key: str) -> SiteRef:
    """Active website by key (one indexed query). Draft/disabled sites are not public."""
    stmt = _with_primary_host(
        select(*_SITE_COLUMNS).where(Website.key == key, Website.status == WebsiteStatus.ACTIVE)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise NotFound(f"Website {key!r} not found", code="site_not_found")
    return _site_ref(row)


async def resolve_host(session: AsyncSession, host: str) -> HostResolution:
    """Map a request Host to its website. Only active domains of active sites resolve."""
    try:
        hostname = normalize_hostname(host)
    except InvalidHostname as exc:
        raise NotFound(str(exc), code="domain_not_found") from exc

    stmt = _with_primary_host(
        select(*_SITE_COLUMNS, WebsiteDomain.is_primary, WebsiteDomain.redirect_to_primary)
        .select_from(WebsiteDomain)
        .join(Website, Website.id == WebsiteDomain.website_id)
        .where(
            WebsiteDomain.hostname == hostname,
            WebsiteDomain.status == DomainStatus.ACTIVE,
            Website.status == WebsiteStatus.ACTIVE,
        )
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise NotFound(f"No active website for host {hostname!r}", code="domain_not_found")

    site = _site_ref(row)
    is_primary: bool = row.is_primary
    redirect_to = None
    if not is_primary and row.redirect_to_primary and site.primary_host not in (None, hostname):
        redirect_to = site.base_url
    return HostResolution(
        site=site, hostname=hostname, is_primary=is_primary, redirect_to=redirect_to
    )


@dataclass(frozen=True, slots=True)
class SiteHosts:
    site: SiteRef
    hosts: list[WebsiteDomain]


async def list_active_sites(session: AsyncSession) -> list[SiteHosts]:
    """All active websites with their active domains (two queries, any number of sites)."""
    rows = await session.execute(
        _with_primary_host(
            select(*_SITE_COLUMNS).where(Website.status == WebsiteStatus.ACTIVE)
        ).order_by(Website.key)
    )
    sites = [_site_ref(r) for r in rows]
    if not sites:
        return []
    domains = await session.scalars(
        select(WebsiteDomain)
        .where(
            WebsiteDomain.website_id.in_([s.id for s in sites]),
            WebsiteDomain.status == DomainStatus.ACTIVE,
        )
        .order_by(WebsiteDomain.is_primary.desc(), WebsiteDomain.hostname)
    )
    by_site: dict[uuid.UUID, list[WebsiteDomain]] = {s.id: [] for s in sites}
    for d in domains:
        by_site[d.website_id].append(d)
    return [SiteHosts(site=s, hosts=by_site[s.id]) for s in sites]
