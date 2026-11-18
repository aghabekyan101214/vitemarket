"""Shared catalog (products, translations) and per-site assignments."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.admin.deps import Ctx
from app.modules.catalog import service
from app.modules.catalog.schemas import (
    AssignmentIn,
    AssignmentOut,
    BulkAssignIn,
    BulkAssignOut,
    ProductCreate,
    ProductOut,
    ProductPage,
    ProductStatusName,
    ProductUpdate,
    ReorderIn,
    TranslationIn,
    TranslationOut,
)
from app.modules.websites.schemas import Locale
from app.modules.websites.service import get_website

products = APIRouter(prefix="/products", tags=["admin: products"])
assignments = APIRouter(prefix="/websites/{website_id}/products", tags=["admin: site products"])


# --- products -------------------------------------------------------------------------


@products.get("", response_model=ProductPage, summary="Search the catalog")
async def list_products(
    ctx: Ctx,
    q: Annotated[str | None, Query(max_length=100, description="SKU or name")] = None,
    product_status: Annotated[ProductStatusName | None, Query(alias="status")] = None,
    website_id: Annotated[uuid.UUID | None, Query(description="Assigned to")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ProductPage:
    if website_id is not None:
        ctx.principal.require_site_read(website_id)
    items, total = await service.list_products(
        ctx.session,
        q=q,
        status=product_status,
        website_id=website_id,
        page=page,
        page_size=page_size,
    )
    return ProductPage(items=items, total=total, page=page, page_size=page_size)


@products.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(ctx: Ctx, body: ProductCreate) -> ProductOut:
    product = await service.create_product(ctx, body)
    await ctx.commit()
    return await service.product_out(ctx.session, product)


@products.get("/{product_id}", response_model=ProductOut)
async def get_product(ctx: Ctx, product_id: uuid.UUID) -> ProductOut:
    return await service.product_out(
        ctx.session, await service.get_product(ctx.session, product_id)
    )


@products.patch("/{product_id}", response_model=ProductOut, summary="Update shared product data")
async def update_product(ctx: Ctx, product_id: uuid.UUID, body: ProductUpdate) -> ProductOut:
    """Republishes every website the product is assigned to."""
    product = await service.get_product(ctx.session, product_id)
    await service.update_product(ctx, product, body)
    await ctx.commit()
    return await service.product_out(ctx.session, product)


@products.post("/{product_id}/archive", response_model=ProductOut)
async def archive_product(ctx: Ctx, product_id: uuid.UUID) -> ProductOut:
    """Hides the product on every website (assignments are kept)."""
    product = await service.get_product(ctx.session, product_id)
    await service.archive_product(ctx, product)
    await ctx.commit()
    return await service.product_out(ctx.session, product)


@products.get("/{product_id}/translations", response_model=list[TranslationOut])
async def list_translations(ctx: Ctx, product_id: uuid.UUID) -> list[TranslationOut]:
    product = await service.get_product(ctx.session, product_id)
    return [
        TranslationOut.model_validate(t)
        for t in await service.list_translations(ctx.session, product)
    ]


@products.put(
    "/{product_id}/translations/{locale}",
    response_model=TranslationOut,
    responses={201: {"description": "Created"}, 409: {"description": "Slug already taken"}},
)
async def put_translation(
    ctx: Ctx, product_id: uuid.UUID, locale: Locale, body: TranslationIn, response: Response
) -> TranslationOut:
    product = await service.get_product(ctx.session, product_id)
    tr, created = await service.put_translation(ctx, product, locale, body)
    await ctx.commit()
    if created:
        response.status_code = status.HTTP_201_CREATED
    return TranslationOut.model_validate(tr)


@products.delete(
    "/{product_id}/translations/{locale}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"description": "Required by a website's default locale"}},
)
async def delete_translation(ctx: Ctx, product_id: uuid.UUID, locale: Locale) -> None:
    product = await service.get_product(ctx.session, product_id)
    await service.delete_translation(ctx, product, locale)
    await ctx.commit()


# --- site assignments (static paths before /{product_id}) -----------------------------


@assignments.get("", response_model=list[AssignmentOut], summary="Products of a website")
async def list_assignments(ctx: Ctx, website_id: uuid.UUID) -> list[AssignmentOut]:
    ctx.principal.require_site_read(website_id)
    website = await get_website(ctx.session, website_id)
    return await service.list_assignments(ctx.session, website)


@assignments.put("/order", status_code=status.HTTP_204_NO_CONTENT, summary="Bulk reorder")
async def reorder(ctx: Ctx, website_id: uuid.UUID, body: ReorderIn) -> None:
    website = await get_website(ctx.session, website_id)
    await service.reorder(ctx, website, body.product_ids)
    await ctx.commit()


@assignments.post("/bulk-assign", response_model=BulkAssignOut)
async def bulk_assign(ctx: Ctx, website_id: uuid.UUID, body: BulkAssignIn) -> BulkAssignOut:
    website = await get_website(ctx.session, website_id)
    result = await service.bulk_assign(ctx, website, body.product_ids, is_visible=body.is_visible)
    await ctx.commit()
    return result


@assignments.put(
    "/{product_id}",
    response_model=AssignmentOut,
    summary="Assign a product / change visibility, featured flag or position",
    responses={422: {"description": "Visible products need a default-locale translation"}},
)
async def put_assignment(
    ctx: Ctx, website_id: uuid.UUID, product_id: uuid.UUID, body: AssignmentIn
) -> AssignmentOut:
    website = await get_website(ctx.session, website_id)
    product = await service.get_product(ctx.session, product_id)
    await service.put_assignment(ctx, website, product, body)
    await ctx.commit()
    rows = await service.list_assignments(ctx.session, website)
    return next(a for a in rows if a.product_id == product_id)


@assignments.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assignment(ctx: Ctx, website_id: uuid.UUID, product_id: uuid.UUID) -> None:
    website = await get_website(ctx.session, website_id)
    product = await service.get_product(ctx.session, product_id)
    await service.delete_assignment(ctx, website, product)
    await ctx.commit()
