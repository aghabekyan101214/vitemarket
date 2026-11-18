"""Admin operations on product images and theme assets.

These functions commit themselves: the object is uploaded *before* the DB write, so a
failure anywhere up to and including the commit must delete the uploaded object again.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import NotFound, ValidationFailed
from app.core.media import public_url
from app.core.storage import Storage
from app.modules.catalog.models import Product, ProductImage
from app.modules.catalog.service import mark_product_changed, product_slugs
from app.modules.context import MutationContext
from app.modules.media import service as media
from app.modules.media.schemas import THEME_ASSET_COLUMNS, AltText, ImageOut
from app.modules.publishing import impact
from app.modules.websites.models import Website, WebsiteTheme
from app.modules.websites.resolution import SiteRef
from app.modules.websites.service import get_theme

POSITION_STEP = 10


def image_out(settings: Settings, img: ProductImage) -> ImageOut:
    return ImageOut(
        id=img.id,
        product_id=img.product_id,
        key=img.storage_key,
        url=public_url(settings, img.storage_key) or "",
        content_type=img.content_type,
        width=img.width,
        height=img.height,
        size_bytes=img.size_bytes,
        position=img.position,
        alt=img.alt,
        created_at=img.created_at,
    )


async def get_image(session: AsyncSession, image_id: uuid.UUID) -> ProductImage:
    img = await session.get(ProductImage, image_id)
    if img is None:
        raise NotFound("Image not found", code="image_not_found")
    return img


async def list_images(session: AsyncSession, product_id: uuid.UUID) -> list[ProductImage]:
    rows = await session.scalars(
        select(ProductImage)
        .where(ProductImage.product_id == product_id)
        .order_by(ProductImage.position, ProductImage.id)
    )
    return list(rows)


async def _republish(ctx: MutationContext, product_id: uuid.UUID) -> None:
    await mark_product_changed(ctx, product_id, [await product_slugs(ctx.session, product_id)])


async def add_product_image(
    ctx: MutationContext, storage: Storage, product: Product, data: bytes, alt: AltText
) -> ProductImage:
    ctx.principal.require_catalog_write()
    image = await media.process_upload(data, ctx.settings)
    key = media.product_image_key(product.id, image.extension)
    await media.store(storage, key, image)
    try:
        last = await ctx.session.scalar(
            select(func.max(ProductImage.position)).where(ProductImage.product_id == product.id)
        )
        row = ProductImage(
            product_id=product.id,
            storage_key=key,
            content_type=image.content_type,
            width=image.width,
            height=image.height,
            size_bytes=len(image.data),
            position=(last or 0) + POSITION_STEP,
            alt=alt,
        )
        ctx.session.add(row)
        await ctx.session.flush()
        await ctx.session.refresh(row)
        ctx.audit(
            "image.create",
            "product_image",
            row.id,
            diff={"product_id": [None, product.id], "key": [None, key]},
        )
        await _republish(ctx, product.id)
        await ctx.commit()
    except BaseException:
        await ctx.session.rollback()
        await media.discard(storage, key)
        raise
    return row


async def update_alt(ctx: MutationContext, img: ProductImage, alt: AltText) -> ProductImage:
    ctx.principal.require_catalog_write()
    before = dict(img.alt)
    img.alt = alt
    await ctx.session.flush()
    if before != alt:
        ctx.audit("image.update", "product_image", img.id, diff={"alt": [before, alt]})
        await _republish(ctx, img.product_id)
    return img


async def reorder_images(
    ctx: MutationContext, product: Product, image_ids: Sequence[uuid.UUID]
) -> list[ProductImage]:
    ctx.principal.require_catalog_write()
    images = {img.id: img for img in await list_images(ctx.session, product.id)}
    if len(set(image_ids)) != len(image_ids) or set(image_ids) != set(images):
        raise ValidationFailed(
            "image_ids must list every image of the product exactly once",
            code="invalid_image_order",
        )
    for index, image_id in enumerate(image_ids, start=1):
        images[image_id].position = index * POSITION_STEP
    await ctx.session.flush()
    ctx.audit(
        "image.reorder",
        "product",
        product.id,
        diff={"order": [None, [str(i) for i in image_ids]]},
    )
    await _republish(ctx, product.id)
    return [images[i] for i in image_ids]


async def delete_image(ctx: MutationContext, img: ProductImage) -> None:
    """The row goes now; the object is purged after the retention period."""
    ctx.principal.require_catalog_write()
    ctx.audit(
        "image.delete",
        "product_image",
        img.id,
        diff={"product_id": [img.product_id, None], "key": [img.storage_key, None]},
    )
    await media.retire(ctx.session, [img.storage_key], ctx.settings)
    product_id = img.product_id
    await ctx.session.delete(img)
    await ctx.session.flush()
    await _republish(ctx, product_id)


# --- theme assets ---------------------------------------------------------------------


async def set_theme_asset(
    ctx: MutationContext, storage: Storage, website: Website, kind: str, data: bytes
) -> WebsiteTheme:
    ctx.principal.require_site_edit(website.id)
    column = THEME_ASSET_COLUMNS[kind]
    image = await media.process_upload(data, ctx.settings)
    key = media.theme_asset_key(website.id, kind, image.extension)
    await media.store(storage, key, image)
    try:
        theme = await get_theme(ctx.session, website)
        previous: str | None = getattr(theme, column)
        setattr(theme, column, key)
        await media.retire(ctx.session, [previous], ctx.settings)
        await ctx.session.flush()
        await ctx.session.refresh(theme)
        ctx.audit(
            "theme.asset",
            "theme",
            website.id,
            website_id=website.id,
            diff={column: [previous, key]},
        )
        ctx.changed(website.id, impact.whole_site(SiteRef.of(website)))
        await ctx.commit()
    except BaseException:
        await ctx.session.rollback()
        await media.discard(storage, key)
        raise
    return theme


async def remove_theme_asset(ctx: MutationContext, website: Website, kind: str) -> WebsiteTheme:
    ctx.principal.require_site_edit(website.id)
    column = THEME_ASSET_COLUMNS[kind]
    theme = await get_theme(ctx.session, website)
    previous: str | None = getattr(theme, column)
    if previous is None:
        raise NotFound(f"No {kind} is set", code="asset_not_found")
    setattr(theme, column, None)
    await media.retire(ctx.session, [previous], ctx.settings)
    await ctx.session.flush()
    await ctx.session.refresh(theme)
    ctx.audit(
        "theme.asset", "theme", website.id, website_id=website.id, diff={column: [previous, None]}
    )
    ctx.changed(website.id, impact.whole_site(SiteRef.of(website)))
    return theme
