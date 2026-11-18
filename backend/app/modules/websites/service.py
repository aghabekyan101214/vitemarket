"""Admin operations on websites, domains and themes.

Services check permissions via `ctx.principal`, mutate, record audit entries and declare
the public impact with `ctx.changed(...)`. Routes call `ctx.commit()`.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, PreconditionFailed, ValidationFailed
from app.modules.audit.service import diff, snapshot
from app.modules.catalog.models import Product, ProductTranslation, WebsiteProduct
from app.modules.context import MutationContext
from app.modules.publishing import impact
from app.modules.websites.dns import TxtResolver, verification_record_name
from app.modules.websites.models import (
    DomainStatus,
    Website,
    WebsiteDomain,
    WebsiteStatus,
    WebsiteTheme,
)
from app.modules.websites.resolution import SiteRef
from app.modules.websites.schemas import (
    DEFAULT_THEME,
    DomainCreate,
    DomainOut,
    DomainUpdate,
    DomainVerification,
    ThemeTokens,
    WebsiteCreate,
    WebsiteOut,
    WebsiteUpdate,
)

WEBSITE_FIELDS = ("name", "country_code", "default_locale", "locales", "auto_publish", "status")
DOMAIN_FIELDS = ("hostname", "is_primary", "status", "redirect_to_primary", "cloudflare_zone_id")


# --- loading --------------------------------------------------------------------------


async def get_website(session: AsyncSession, website_id: uuid.UUID) -> Website:
    website = await session.get(Website, website_id)
    if website is None:
        raise NotFound("Website not found", code="website_not_found")
    return website


async def get_domain(session: AsyncSession, domain_id: uuid.UUID) -> WebsiteDomain:
    domain = await session.get(WebsiteDomain, domain_id)
    if domain is None:
        raise NotFound("Domain not found", code="domain_not_found")
    return domain


async def _primary_hosts(
    session: AsyncSession, website_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    rows = await session.execute(
        select(WebsiteDomain.website_id, WebsiteDomain.hostname).where(
            WebsiteDomain.website_id.in_(list(website_ids)), WebsiteDomain.is_primary
        )
    )
    return {wid: host for wid, host in rows}


async def website_out(session: AsyncSession, website: Website) -> WebsiteOut:
    await session.refresh(website)  # content_version/updated_at are set by the database
    hosts = await _primary_hosts(session, [website.id])
    return WebsiteOut.model_validate(website).model_copy(
        update={"primary_host": hosts.get(website.id)}
    )


def domain_out(domain: WebsiteDomain) -> DomainOut:
    return DomainOut(
        id=domain.id,
        website_id=domain.website_id,
        hostname=domain.hostname,
        is_primary=domain.is_primary,
        status=domain.status.value,
        redirect_to_primary=domain.redirect_to_primary,
        cloudflare_zone_id=domain.cloudflare_zone_id,
        verified_at=domain.verified_at,
        verification=DomainVerification(
            record_name=verification_record_name(domain.hostname),
            value=domain.verification_token,
        ),
    )


# --- websites -------------------------------------------------------------------------


async def list_websites(ctx: MutationContext) -> list[WebsiteOut]:
    stmt = select(Website).order_by(Website.key)
    if (ids := ctx.principal.readable_site_ids) is not None:
        stmt = stmt.where(Website.id.in_(ids))
    websites = list(await ctx.session.scalars(stmt))
    hosts = await _primary_hosts(ctx.session, [w.id for w in websites])
    return [
        WebsiteOut.model_validate(w).model_copy(update={"primary_host": hosts.get(w.id)})
        for w in websites
    ]


async def create_website(ctx: MutationContext, data: WebsiteCreate) -> Website:
    ctx.principal.require_admin()
    if await ctx.session.scalar(select(Website.id).where(Website.key == data.key)) is not None:
        raise Conflict(f"Website key {data.key!r} is already taken", code="website_exists")
    website = Website(
        key=data.key,
        name=data.name,
        country_code=data.country_code,
        default_locale=data.default_locale,
        locales=data.locales,
        auto_publish=data.auto_publish,
        status=WebsiteStatus.DRAFT,
    )
    ctx.session.add(website)
    await ctx.session.flush()
    tokens = (data.theme or ThemeTokens.model_validate(DEFAULT_THEME)).model_dump(mode="json")
    ctx.session.add(WebsiteTheme(website_id=website.id, tokens=tokens))
    await ctx.session.flush()
    await ctx.session.refresh(website)
    ctx.audit(
        "website.create",
        "website",
        website.id,
        website_id=website.id,
        diff=diff(None, {"key": website.key, **snapshot(website, WEBSITE_FIELDS)}),
    )
    return website


async def _products_missing_translation(
    session: AsyncSession, website_id: uuid.UUID, locale: str
) -> list[str]:
    """SKUs visible on the site without a translation in `locale`."""
    rows = await session.scalars(
        select(Product.sku)
        .join(WebsiteProduct, WebsiteProduct.product_id == Product.id)
        .where(
            WebsiteProduct.website_id == website_id,
            WebsiteProduct.is_visible.is_(True),
            ~exists().where(
                and_(
                    ProductTranslation.product_id == Product.id, ProductTranslation.locale == locale
                )
            ),
        )
        .order_by(Product.sku)
    )
    return list(rows)


async def update_website(ctx: MutationContext, website: Website, data: WebsiteUpdate) -> Website:
    ctx.principal.require_admin()
    before = snapshot(website, WEBSITE_FIELDS)
    values: dict[str, Any] = data.model_dump(exclude_unset=True)
    for key, value in values.items():
        if value is None:
            raise ValidationFailed(f"`{key}` cannot be null")
    new_default = values.get("default_locale", website.default_locale)
    new_locales = values.get("locales", website.locales)
    if new_default not in new_locales:
        raise ValidationFailed("default_locale must be one of locales", code="invalid_locales")
    if new_default != website.default_locale:
        missing = await _products_missing_translation(ctx.session, website.id, new_default)
        if missing:
            raise Conflict(
                f"Visible products lack a {new_default!r} translation",
                code="default_translation_missing",
                extra={"skus": missing},
            )
    for key, value in values.items():
        setattr(website, key, value)
    await ctx.session.flush()
    changes = diff(before, snapshot(website, WEBSITE_FIELDS))
    if changes:
        ctx.audit("website.update", "website", website.id, website_id=website.id, diff=changes)
        ctx.changed(website.id, impact.whole_site(SiteRef.of(website), routing=True))
    return website


async def set_website_status(
    ctx: MutationContext, website: Website, status: WebsiteStatus
) -> Website:
    ctx.principal.require_admin()
    if website.status is status:
        return website
    if status is WebsiteStatus.ACTIVE:
        primary_active = await ctx.session.scalar(
            select(
                exists().where(
                    WebsiteDomain.website_id == website.id,
                    WebsiteDomain.is_primary,
                    WebsiteDomain.status == DomainStatus.ACTIVE,
                )
            )
        )
        if not primary_active:
            raise Conflict(
                "Activate (verify) a primary domain before enabling the website",
                code="primary_domain_required",
            )
    before = website.status
    website.status = status
    await ctx.session.flush()
    ctx.audit(
        f"website.{'enable' if status is WebsiteStatus.ACTIVE else 'disable'}",
        "website",
        website.id,
        website_id=website.id,
        diff={"status": [before.value, status.value]},
    )
    ctx.changed(website.id, impact.whole_site(SiteRef.of(website), routing=True))
    return website


# --- domains --------------------------------------------------------------------------


async def list_domains(session: AsyncSession, website_id: uuid.UUID) -> list[WebsiteDomain]:
    rows = await session.scalars(
        select(WebsiteDomain)
        .where(WebsiteDomain.website_id == website_id)
        .order_by(WebsiteDomain.is_primary.desc(), WebsiteDomain.hostname)
    )
    return list(rows)


async def _current_primary(session: AsyncSession, website_id: uuid.UUID) -> WebsiteDomain | None:
    return await session.scalar(
        select(WebsiteDomain).where(
            WebsiteDomain.website_id == website_id, WebsiteDomain.is_primary
        )
    )


async def _switch_primary(session: AsyncSession, domain: WebsiteDomain) -> None:
    current = await _current_primary(session, domain.website_id)
    if current is not None and current.id != domain.id:
        current.is_primary = False
        await session.flush()  # partial unique index: clear the old primary first
    domain.is_primary = True
    await session.flush()


def _domain_changed(ctx: MutationContext, website: Website) -> None:
    ctx.changed(website.id, impact.whole_site(SiteRef.of(website), routing=True))


async def add_domain(ctx: MutationContext, website: Website, data: DomainCreate) -> WebsiteDomain:
    ctx.principal.require_site_edit(website.id)
    existing = await ctx.session.scalar(
        select(WebsiteDomain.website_id).where(WebsiteDomain.hostname == data.hostname)
    )
    if existing is not None:
        raise Conflict(f"{data.hostname} is already registered", code="domain_exists")
    current_primary = await _current_primary(ctx.session, website.id)
    make_it_primary = data.is_primary or current_primary is None
    if make_it_primary and current_primary is not None and website.status is WebsiteStatus.ACTIVE:
        raise Conflict(
            "A new domain is unverified; add it, verify it, then make it primary",
            code="domain_not_active",
        )
    domain = WebsiteDomain(
        website_id=website.id,
        hostname=data.hostname,
        status=DomainStatus.PENDING,
        redirect_to_primary=data.redirect_to_primary,
        cloudflare_zone_id=data.cloudflare_zone_id,
    )
    ctx.session.add(domain)
    await ctx.session.flush()
    if make_it_primary:
        await _switch_primary(ctx.session, domain)
    await ctx.session.refresh(domain)
    ctx.audit(
        "domain.create",
        "domain",
        domain.id,
        website_id=website.id,
        diff=diff(None, snapshot(domain, DOMAIN_FIELDS)),
    )
    _domain_changed(ctx, website)
    return domain


def _guard_primary_of_active_site(website: Website, domain: WebsiteDomain, action: str) -> None:
    if domain.is_primary and website.status is WebsiteStatus.ACTIVE:
        raise Conflict(
            f"Cannot {action} the primary domain of an active website; make another domain "
            "primary first",
            code="primary_domain_in_use",
        )


async def update_domain(
    ctx: MutationContext, website: Website, domain: WebsiteDomain, data: DomainUpdate
) -> WebsiteDomain:
    ctx.principal.require_site_edit(website.id)
    before = snapshot(domain, DOMAIN_FIELDS)
    values = data.model_dump(exclude_unset=True)
    if "status" in values and values["status"] != domain.status.value:
        _guard_primary_of_active_site(website, domain, "deactivate")
        domain.status = DomainStatus(values.pop("status"))
        domain.verified_at = None
    values.pop("status", None)
    for key, value in values.items():
        if key == "redirect_to_primary" and value is None:
            raise ValidationFailed("`redirect_to_primary` cannot be null")
        setattr(domain, key, value)
    await ctx.session.flush()
    changes = diff(before, snapshot(domain, DOMAIN_FIELDS))
    if changes:
        ctx.audit("domain.update", "domain", domain.id, website_id=website.id, diff=changes)
        _domain_changed(ctx, website)
    return domain


async def delete_domain(ctx: MutationContext, website: Website, domain: WebsiteDomain) -> None:
    ctx.principal.require_site_edit(website.id)
    _guard_primary_of_active_site(website, domain, "delete")
    ctx.audit(
        "domain.delete",
        "domain",
        domain.id,
        website_id=website.id,
        diff=diff(snapshot(domain, DOMAIN_FIELDS), None),
    )
    await ctx.session.delete(domain)
    await ctx.session.flush()
    _domain_changed(ctx, website)


async def make_primary(
    ctx: MutationContext, website: Website, domain: WebsiteDomain
) -> WebsiteDomain:
    ctx.principal.require_site_edit(website.id)
    if domain.is_primary:
        return domain
    if website.status is WebsiteStatus.ACTIVE and domain.status is not DomainStatus.ACTIVE:
        raise Conflict(
            "Only a verified (active) domain can become primary of an active website",
            code="domain_not_active",
        )
    previous = await _current_primary(ctx.session, website.id)
    await _switch_primary(ctx.session, domain)
    ctx.audit(
        "domain.make_primary",
        "domain",
        domain.id,
        website_id=website.id,
        diff={"primary": [previous.hostname if previous else None, domain.hostname]},
    )
    _domain_changed(ctx, website)
    return domain


async def verify_domain(
    ctx: MutationContext,
    website: Website,
    domain: WebsiteDomain,
    resolver: TxtResolver,
    *,
    force: bool = False,
) -> WebsiteDomain:
    """Activate a domain after checking `_showcase-verify.<host>` TXT = token (admin)."""
    ctx.principal.require_admin()
    if domain.status is DomainStatus.ACTIVE:
        return domain
    if not force:
        name = verification_record_name(domain.hostname)
        records = await resolver.txt(name)
        if domain.verification_token not in (r.strip() for r in records):
            raise PreconditionFailed(
                f"TXT record {name} does not contain the verification token",
                code="domain_verification_failed",
                extra={"record_name": name, "expected": domain.verification_token},
            )
    domain.status = DomainStatus.ACTIVE
    domain.verified_at = datetime.now(UTC)
    await ctx.session.flush()
    ctx.audit(
        "domain.verify",
        "domain",
        domain.id,
        website_id=website.id,
        diff={"status": ["pending", "active"], "forced": [None, force]},
    )
    _domain_changed(ctx, website)
    return domain


# --- theme ----------------------------------------------------------------------------


async def get_theme(session: AsyncSession, website: Website) -> WebsiteTheme:
    theme = await session.get(WebsiteTheme, website.id)
    if theme is None:  # websites created outside the API (e.g. raw SQL)
        theme = WebsiteTheme(website_id=website.id, tokens=DEFAULT_THEME)
        session.add(theme)
        await session.flush()
        await session.refresh(theme)
    return theme


async def update_theme(
    ctx: MutationContext, website: Website, tokens: ThemeTokens, *, if_match: int
) -> WebsiteTheme:
    ctx.principal.require_site_edit(website.id)
    await get_theme(ctx.session, website)  # ensure the row exists
    theme = await ctx.session.scalar(
        select(WebsiteTheme)
        .where(WebsiteTheme.website_id == website.id)
        .with_for_update()  # serialize concurrent editors for the version check
        .execution_options(populate_existing=True)
    )
    assert theme is not None
    if theme.version != if_match:
        raise PreconditionFailed(
            "The theme was changed by someone else; reload and retry",
            code="version_mismatch",
            extra={"current_version": theme.version},
        )
    before = theme.tokens
    theme.tokens = tokens.model_dump(mode="json")
    theme.version = theme.version + 1
    theme.updated_at = func.now()
    await ctx.session.flush()
    await ctx.session.refresh(theme)
    ctx.audit(
        "theme.update",
        "theme",
        website.id,
        website_id=website.id,
        diff={"tokens": [before, theme.tokens]},
    )
    ctx.changed(website.id, impact.whole_site(SiteRef.of(website)))
    return theme
