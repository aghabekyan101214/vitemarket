"""Page content (optimistic locking) and SEO overrides."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.admin.deps import Ctx, IfMatch, version_etag
from app.core.errors import NotFound
from app.modules.content import service
from app.modules.content.models import PageType
from app.modules.content.schemas import (
    ContentOut,
    ContentSummary,
    ContentUpdate,
    EditablePage,
    SeoFields,
    SeoOut,
    SeoPageName,
)
from app.modules.websites.schemas import Locale
from app.modules.websites.service import get_website

router = APIRouter(prefix="/websites/{website_id}", tags=["admin: content & SEO"])


@router.get("/content", response_model=list[ContentSummary], summary="All pages and locales")
async def list_content(ctx: Ctx, website_id: uuid.UUID) -> list[ContentSummary]:
    ctx.principal.require_site_read(website_id)
    await get_website(ctx.session, website_id)
    return [
        ContentSummary.model_validate(c)
        for c in await service.list_content(ctx.session, website_id)
    ]


@router.get("/content/{page}/{locale}", response_model=ContentOut, summary="ETag = version")
async def get_content(
    ctx: Ctx, website_id: uuid.UUID, page: EditablePage, locale: Locale, response: Response
) -> ContentOut:
    ctx.principal.require_site_read(website_id)
    content = await service.get_content(ctx.session, website_id, PageType(page), locale)
    if content is None:
        raise NotFound("No content for this page and locale", code="content_not_found")
    response.headers["ETag"] = version_etag(content.version)
    return ContentOut.model_validate(content)


@router.put(
    "/content/{page}/{locale}",
    response_model=ContentOut,
    summary="Create or replace page content",
    responses={
        201: {"description": "Created"},
        412: {"description": "Version mismatch (someone else saved first)"},
        428: {"description": "If-Match missing for existing content"},
    },
)
async def put_content(
    ctx: Ctx,
    website_id: uuid.UUID,
    page: EditablePage,
    locale: Locale,
    body: ContentUpdate,
    if_match: IfMatch,
    response: Response,
) -> ContentOut:
    """Updating existing content requires `If-Match: "<version>"` from the last GET.
    Rich text in `text` blocks is sanitized."""
    website = await get_website(ctx.session, website_id)
    content, created = await service.put_content(
        ctx, website, PageType(page), locale, body, if_match=if_match
    )
    await ctx.commit()
    response.headers["ETag"] = version_etag(content.version)
    if created:
        response.status_code = status.HTTP_201_CREATED
    return ContentOut.model_validate(content)


@router.get("/seo", response_model=list[SeoOut], summary="SEO overrides")
async def list_seo(
    ctx: Ctx, website_id: uuid.UUID, locale: Annotated[Locale | None, Query()] = None
) -> list[SeoOut]:
    ctx.principal.require_site_read(website_id)
    await get_website(ctx.session, website_id)
    return [
        SeoOut.model_validate(r) for r in await service.list_seo(ctx.session, website_id, locale)
    ]


ProductIdQuery = Annotated[
    uuid.UUID | None, Query(description="Required for page=product, forbidden otherwise")
]


@router.put("/seo/{page}/{locale}", response_model=SeoOut, summary="Set an SEO override")
async def put_seo(
    ctx: Ctx,
    website_id: uuid.UUID,
    page: SeoPageName,
    locale: Locale,
    body: SeoFields,
    product_id: ProductIdQuery = None,
) -> SeoOut:
    """Empty fields fall back to content-derived values on the public API."""
    website = await get_website(ctx.session, website_id)
    row = await service.put_seo(ctx, website, PageType(page), locale, product_id, body)
    await ctx.commit()
    return SeoOut.model_validate(row)


@router.delete("/seo/{page}/{locale}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_seo(
    ctx: Ctx,
    website_id: uuid.UUID,
    page: SeoPageName,
    locale: Locale,
    product_id: ProductIdQuery = None,
) -> None:
    website = await get_website(ctx.session, website_id)
    await service.delete_seo(ctx, website, PageType(page), locale, product_id)
    await ctx.commit()
