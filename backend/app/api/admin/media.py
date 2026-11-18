"""Product images and theme assets (multipart uploads)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile, status
from pydantic import ValidationError

from app.api.admin.deps import Ctx
from app.api.deps import StorageDep
from app.core.errors import ValidationFailed
from app.modules.catalog.service import get_product
from app.modules.media import admin as service
from app.modules.media.images import PayloadTooLarge
from app.modules.media.schemas import (
    AltText,
    ImageAltUpdate,
    ImageOrderIn,
    ImageOut,
    ThemeAssetKind,
    alt_adapter,
)
from app.modules.websites.schemas import ThemeOut
from app.modules.websites.service import get_website

router = APIRouter(tags=["admin: media"])

_MULTIPART_OVERHEAD = 64 * 1024
_UPLOAD_RESPONSES: dict[int | str, dict[str, object]] = {
    413: {"description": "File larger than UPLOAD_MAX_BYTES"},
    415: {"description": "Not a JPEG/PNG/WebP/AVIF image, animated, corrupt or too many pixels"},
}


async def _read_upload(request: Request, ctx: Ctx, file: UploadFile) -> bytes:
    limit = ctx.settings.upload_max_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + _MULTIPART_OVERHEAD:
        raise PayloadTooLarge(f"Images must be at most {limit // (1024 * 1024)} MB")
    data = await file.read(limit + 1)  # one byte more than allowed → detect oversize
    if len(data) > limit:
        raise PayloadTooLarge(f"Images must be at most {limit // (1024 * 1024)} MB")
    return data


def _parse_alt(raw: str | None) -> AltText:
    if not raw:
        return {}
    try:
        return alt_adapter.validate_json(raw)
    except ValidationError as exc:
        raise ValidationFailed(
            'alt must be a JSON object of locale → text, e.g. {"en": "Oak chair"}',
            code="invalid_alt",
        ) from exc


# --- product images -------------------------------------------------------------------


@router.get("/products/{product_id}/images", response_model=list[ImageOut])
async def list_images(ctx: Ctx, product_id: uuid.UUID) -> list[ImageOut]:
    await get_product(ctx.session, product_id)
    return [
        service.image_out(ctx.settings, img)
        for img in await service.list_images(ctx.session, product_id)
    ]


@router.post(
    "/products/{product_id}/images",
    response_model=ImageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a product image",
    responses=_UPLOAD_RESPONSES,
)
async def upload_image(
    request: Request,
    ctx: Ctx,
    storage: StorageDep,
    product_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="JPEG, PNG, WebP or AVIF")],
    alt: Annotated[
        str | None, Form(description='JSON object, e.g. {"en": "Oak chair", "de": "Eichenstuhl"}')
    ] = None,
) -> ImageOut:
    """Metadata (EXIF/GPS) is stripped and orientation applied. Appended after existing
    images; the object key is immutable and cached for a year."""
    ctx.principal.require_catalog_write()  # before reading the body
    product = await get_product(ctx.session, product_id)
    alt_text = _parse_alt(alt)
    data = await _read_upload(request, ctx, file)
    img = await service.add_product_image(ctx, storage, product, data, alt_text)
    return service.image_out(ctx.settings, img)


@router.put(
    "/products/{product_id}/images/order",
    response_model=list[ImageOut],
    summary="Reorder images (first = primary image)",
)
async def reorder_images(ctx: Ctx, product_id: uuid.UUID, body: ImageOrderIn) -> list[ImageOut]:
    product = await get_product(ctx.session, product_id)
    images = await service.reorder_images(ctx, product, body.image_ids)
    await ctx.commit()
    return [service.image_out(ctx.settings, img) for img in images]


@router.patch("/images/{image_id}", response_model=ImageOut, summary="Update alt texts")
async def update_image(ctx: Ctx, image_id: uuid.UUID, body: ImageAltUpdate) -> ImageOut:
    img = await service.get_image(ctx.session, image_id)
    await service.update_alt(ctx, img, body.alt)
    await ctx.commit()
    return service.image_out(ctx.settings, img)


@router.delete("/images/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_image(ctx: Ctx, image_id: uuid.UUID) -> None:
    """The object itself is deleted by the GC sweep after MEDIA_RETENTION_DAYS."""
    img = await service.get_image(ctx.session, image_id)
    await service.delete_image(ctx, img)
    await ctx.commit()


# --- theme assets ---------------------------------------------------------------------


@router.post(
    "/websites/{website_id}/theme/assets",
    response_model=ThemeOut,
    summary="Upload a logo, dark logo, favicon or default social image",
    responses=_UPLOAD_RESPONSES,
)
async def upload_theme_asset(
    request: Request,
    ctx: Ctx,
    storage: StorageDep,
    website_id: uuid.UUID,
    kind: Annotated[ThemeAssetKind, Form()],
    file: Annotated[UploadFile, File(description="JPEG, PNG, WebP or AVIF (no SVG)")],
) -> ThemeOut:
    website = await get_website(ctx.session, website_id)
    ctx.principal.require_site_edit(website.id)
    data = await _read_upload(request, ctx, file)
    theme = await service.set_theme_asset(ctx, storage, website, kind, data)
    return ThemeOut.model_validate(theme)


@router.delete("/websites/{website_id}/theme/assets/{kind}", response_model=ThemeOut)
async def delete_theme_asset(ctx: Ctx, website_id: uuid.UUID, kind: ThemeAssetKind) -> ThemeOut:
    website = await get_website(ctx.session, website_id)
    theme = await service.remove_theme_asset(ctx, website, kind)
    await ctx.commit()
    return ThemeOut.model_validate(theme)
