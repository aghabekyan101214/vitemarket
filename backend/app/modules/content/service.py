"""Admin operations on page content and SEO overrides."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, PreconditionFailed, PreconditionRequired, ValidationFailed
from app.modules.audit.service import diff, snapshot
from app.modules.catalog.models import ProductTranslation, WebsiteProduct
from app.modules.content.models import PageType, SeoMetadata, WebsiteContent
from app.modules.content.schemas import ContentUpdate, SeoFields, content_blocks_adapter
from app.modules.context import MutationContext
from app.modules.publishing import impact
from app.modules.websites.models import Website
from app.modules.websites.resolution import SiteRef

SEO_FIELDS = tuple(SeoFields.model_fields)


def require_site_locale(website: Website, locale: str) -> None:
    if locale not in website.locales:
        raise ValidationFailed(
            f"Locale {locale!r} is not enabled for this website",
            code="locale_not_enabled",
            extra={"locales": website.locales},
        )


# --- content --------------------------------------------------------------------------


async def list_content(session: AsyncSession, website_id: uuid.UUID) -> list[WebsiteContent]:
    rows = await session.scalars(
        select(WebsiteContent)
        .where(WebsiteContent.website_id == website_id)
        .order_by(WebsiteContent.page, WebsiteContent.locale)
    )
    return list(rows)


async def get_content(
    session: AsyncSession, website_id: uuid.UUID, page: PageType, locale: str, *, lock: bool = False
) -> WebsiteContent | None:
    stmt = select(WebsiteContent).where(
        WebsiteContent.website_id == website_id,
        WebsiteContent.page == page,
        WebsiteContent.locale == locale,
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(stmt)


async def put_content(
    ctx: MutationContext,
    website: Website,
    page: PageType,
    locale: str,
    data: ContentUpdate,
    *,
    if_match: int | None,
) -> tuple[WebsiteContent, bool]:
    """Create or replace a page's content. Replacing requires `If-Match: "<version>"`."""
    ctx.principal.require_site_edit(website.id)
    require_site_locale(website, locale)
    blocks: list[dict[str, Any]] = content_blocks_adapter.dump_python(
        data.blocks, mode="json", exclude_none=True
    )
    content = await get_content(ctx.session, website.id, page, locale, lock=True)
    created = content is None
    if content is None:
        content = WebsiteContent(website_id=website.id, page=page, locale=locale, version=1)
        before = None
        ctx.session.add(content)
    else:
        if if_match is None:
            raise PreconditionRequired(
                'Send If-Match: "<version>" to update existing content',
                code="if_match_required",
                extra={"current_version": content.version},
            )
        if content.version != if_match:
            raise PreconditionFailed(
                "The content was changed by someone else; reload and retry",
                code="version_mismatch",
                extra={"current_version": content.version},
            )
        before = snapshot(content, ("title", "subtitle", "blocks"))
        content.version += 1
    content.title = data.title
    content.subtitle = data.subtitle
    content.blocks = blocks
    await ctx.session.flush()
    await ctx.session.refresh(content)
    ctx.audit(
        "content.create" if created else "content.update",
        "content",
        content.id,
        website_id=website.id,
        diff={
            **diff(before, snapshot(content, ("title", "subtitle", "blocks"))),
            "page": page.value,
            "locale": locale,
        },
    )
    ctx.changed(website.id, impact.content_page(SiteRef.of(website), page.value, locale))
    return content, created


# --- SEO ------------------------------------------------------------------------------


async def list_seo(
    session: AsyncSession, website_id: uuid.UUID, locale: str | None
) -> list[SeoMetadata]:
    stmt = select(SeoMetadata).where(SeoMetadata.website_id == website_id)
    if locale:
        stmt = stmt.where(SeoMetadata.locale == locale)
    rows = await session.scalars(
        stmt.order_by(SeoMetadata.locale, SeoMetadata.page, SeoMetadata.product_id)
    )
    return list(rows)


async def _seo_target(
    session: AsyncSession,
    website: Website,
    page: PageType,
    locale: str,
    product_id: uuid.UUID | None,
) -> tuple[impact.Change, SeoMetadata | None]:
    require_site_locale(website, locale)
    site = SiteRef.of(website)
    slugs: dict[str, str] = {}
    if page is PageType.PRODUCT:
        if product_id is None:
            raise ValidationFailed("`product_id` is required for page=product")
        assigned = await session.scalar(
            select(WebsiteProduct.product_id).where(
                WebsiteProduct.website_id == website.id, WebsiteProduct.product_id == product_id
            )
        )
        if assigned is None:
            raise NotFound("Product is not assigned to this website", code="product_not_assigned")
        rows = await session.execute(
            select(ProductTranslation.locale, ProductTranslation.slug).where(
                ProductTranslation.product_id == product_id
            )
        )
        slugs = {loc: slug for loc, slug in rows}
    elif product_id is not None:
        raise ValidationFailed("`product_id` is only allowed for page=product")

    row = await session.scalar(
        select(SeoMetadata).where(
            SeoMetadata.website_id == website.id,
            SeoMetadata.page == page,
            SeoMetadata.locale == locale,
            SeoMetadata.product_id.is_(None)
            if product_id is None
            else SeoMetadata.product_id == product_id,
        )
    )
    change = impact.seo(site, page.value, locale, product_id=product_id, slugs=slugs)
    return change, row


async def put_seo(
    ctx: MutationContext,
    website: Website,
    page: PageType,
    locale: str,
    product_id: uuid.UUID | None,
    data: SeoFields,
) -> SeoMetadata:
    ctx.principal.require_site_edit(website.id)
    change, row = await _seo_target(ctx.session, website, page, locale, product_id)
    before = snapshot(row, SEO_FIELDS) if row else None
    if row is None:
        row = SeoMetadata(website_id=website.id, page=page, locale=locale, product_id=product_id)
        ctx.session.add(row)
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    await ctx.session.flush()
    await ctx.session.refresh(row)
    ctx.audit(
        "seo.update" if before else "seo.create",
        "seo",
        row.id,
        website_id=website.id,
        diff=diff(before, snapshot(row, SEO_FIELDS)),
    )
    ctx.changed(website.id, change)
    return row


async def delete_seo(
    ctx: MutationContext,
    website: Website,
    page: PageType,
    locale: str,
    product_id: uuid.UUID | None,
) -> None:
    ctx.principal.require_site_edit(website.id)
    change, row = await _seo_target(ctx.session, website, page, locale, product_id)
    if row is None:
        raise NotFound("No SEO override for this page", code="seo_not_found")
    ctx.audit(
        "seo.delete",
        "seo",
        row.id,
        website_id=website.id,
        diff=diff(snapshot(row, SEO_FIELDS), None),
    )
    await ctx.session.delete(row)
    await ctx.session.flush()
    ctx.changed(website.id, change)
