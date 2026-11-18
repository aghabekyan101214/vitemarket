"""Admin operations on the shared catalog and per-site product assignments.

Products are shared: an edit is published to *every* website the product is assigned to.
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.slugs import slugify
from app.modules.audit.service import diff, snapshot
from app.modules.catalog.models import (
    Product,
    ProductStatus,
    ProductTranslation,
    WebsiteProduct,
)
from app.modules.catalog.schemas import (
    AssignmentIn,
    AssignmentOut,
    BulkAssignOut,
    ProductAssignmentRef,
    ProductCreate,
    ProductListItem,
    ProductOut,
    ProductUpdate,
    TranslationIn,
    TranslationOut,
)
from app.modules.context import MutationContext
from app.modules.publishing import impact
from app.modules.websites.models import Website
from app.modules.websites.resolution import SiteRef

PRODUCT_FIELDS = ("sku", "status", "attributes")
TRANSLATION_FIELDS = ("name", "slug", "short_description", "description", "attributes")
ASSIGNMENT_FIELDS = ("position", "is_visible", "is_featured")
POSITION_STEP = 10

# ======================================================================================
# Shared helpers
# ======================================================================================


async def get_product(session: AsyncSession, product_id: uuid.UUID) -> Product:
    product = await session.get(Product, product_id)
    if product is None:
        raise NotFound("Product not found", code="product_not_found")
    return product


async def _translations(session: AsyncSession, product_id: uuid.UUID) -> list[ProductTranslation]:
    rows = await session.scalars(
        select(ProductTranslation)
        .where(ProductTranslation.product_id == product_id)
        .order_by(ProductTranslation.locale)
    )
    return list(rows)


async def product_slugs(session: AsyncSession, product_id: uuid.UUID) -> dict[str, str]:
    rows = await session.execute(
        select(ProductTranslation.locale, ProductTranslation.slug).where(
            ProductTranslation.product_id == product_id
        )
    )
    return {loc: slug for loc, slug in rows}


async def _assigned_sites(
    session: AsyncSession, product_id: uuid.UUID
) -> list[tuple[SiteRef, WebsiteProduct]]:
    rows = await session.execute(
        select(Website, WebsiteProduct)
        .join(WebsiteProduct, WebsiteProduct.website_id == Website.id)
        .where(WebsiteProduct.product_id == product_id)
    )
    return [(SiteRef.of(w), wp) for w, wp in rows]


async def mark_product_changed(
    ctx: MutationContext,
    product_id: uuid.UUID,
    slug_versions: Iterable[Mapping[str, str]],
) -> None:
    """Queue regeneration on every site that uses the product (old and new slugs)."""
    slug_versions = list(slug_versions)
    for site, link in await _assigned_sites(ctx.session, product_id):
        change = impact.Change()
        for slugs in slug_versions:
            change.merge(impact.product(site, product_id, slugs, featured=link.is_featured))
        ctx.changed(site.id, change)


async def _check_slug_free(
    session: AsyncSession, product_id: uuid.UUID, locale: str, slug: str
) -> None:
    owner = await session.scalar(
        select(ProductTranslation.product_id).where(
            ProductTranslation.locale == locale,
            ProductTranslation.slug == slug,
            ProductTranslation.product_id != product_id,
        )
    )
    if owner is not None:
        raise Conflict(
            f"Slug {slug!r} is already used by another product in {locale!r}",
            code="slug_taken",
            extra={"locale": locale, "slug": slug},
        )


async def _check_sku_free(session: AsyncSession, sku: str, product_id: uuid.UUID | None) -> None:
    stmt = select(Product.id).where(func.lower(Product.sku) == sku.lower())
    if product_id is not None:
        stmt = stmt.where(Product.id != product_id)
    if await session.scalar(stmt) is not None:
        raise Conflict(f"SKU {sku!r} already exists", code="sku_taken")


# ======================================================================================
# Products
# ======================================================================================


async def list_products(
    session: AsyncSession,
    *,
    q: str | None,
    status: str | None,
    website_id: uuid.UUID | None,
    page: int,
    page_size: int,
) -> tuple[list[ProductListItem], int]:
    stmt = select(Product, func.count().over().label("total"))
    if q:
        pattern = f"%{q.strip().replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(
            or_(
                Product.sku.ilike(pattern),
                exists().where(
                    ProductTranslation.product_id == Product.id,
                    ProductTranslation.name.ilike(pattern),
                ),
            )
        )
    if status:
        stmt = stmt.where(Product.status == ProductStatus(status))
    if website_id:
        stmt = stmt.where(
            exists().where(
                WebsiteProduct.product_id == Product.id, WebsiteProduct.website_id == website_id
            )
        )
    rows = (
        await session.execute(
            stmt.order_by(Product.sku).limit(page_size).offset((page - 1) * page_size)
        )
    ).all()
    products = [r[0] for r in rows]
    total = rows[0].total if rows else 0
    names: dict[uuid.UUID, dict[str, str]] = {p.id: {} for p in products}
    if products:
        for pid, loc, name in await session.execute(
            select(
                ProductTranslation.product_id, ProductTranslation.locale, ProductTranslation.name
            )
            .where(ProductTranslation.product_id.in_(list(names)))
            .order_by(ProductTranslation.locale)
        ):
            names[pid][loc] = name
    items = [
        ProductListItem(
            id=p.id, sku=p.sku, status=p.status.value, names=names[p.id], updated_at=p.updated_at
        )
        for p in products
    ]
    return items, total


async def product_out(session: AsyncSession, product: Product) -> ProductOut:
    translations = await _translations(session, product.id)
    links = await session.execute(
        select(WebsiteProduct, Website.key)
        .join(Website, Website.id == WebsiteProduct.website_id)
        .where(WebsiteProduct.product_id == product.id)
        .order_by(Website.key)
    )
    return ProductOut(
        id=product.id,
        sku=product.sku,
        status=product.status.value,
        attributes=product.attributes,
        created_at=product.created_at,
        updated_at=product.updated_at,
        translations=[TranslationOut.model_validate(t) for t in translations],
        websites=[
            ProductAssignmentRef(
                website_id=wp.website_id,
                website_key=key,
                position=wp.position,
                is_visible=wp.is_visible,
                is_featured=wp.is_featured,
            )
            for wp, key in links
        ],
    )


def _translation_values(data: TranslationIn) -> dict[str, Any]:
    values = data.model_dump()
    values["slug"] = data.slug or slugify(data.name)
    return values


async def create_product(ctx: MutationContext, data: ProductCreate) -> Product:
    ctx.principal.require_catalog_write()
    await _check_sku_free(ctx.session, data.sku, None)
    product = Product(sku=data.sku, attributes=data.attributes)
    ctx.session.add(product)
    await ctx.session.flush()
    for locale, tr in data.translations.items():
        values = _translation_values(tr)
        await _check_slug_free(ctx.session, product.id, locale, values["slug"])
        ctx.session.add(ProductTranslation(product_id=product.id, locale=locale, **values))
    await ctx.session.flush()
    await ctx.session.refresh(product)
    ctx.audit(
        "product.create",
        "product",
        product.id,
        diff=diff(
            None, {**snapshot(product, PRODUCT_FIELDS), "locales": sorted(data.translations)}
        ),
    )
    return product  # not assigned anywhere yet → nothing to publish


async def update_product(ctx: MutationContext, product: Product, data: ProductUpdate) -> Product:
    ctx.principal.require_catalog_write()
    before = snapshot(product, PRODUCT_FIELDS)
    values = data.model_dump(exclude_unset=True)
    for key, value in values.items():
        if value is None:
            raise ValidationFailed(f"`{key}` cannot be null")
    if "sku" in values and values["sku"] != product.sku:
        await _check_sku_free(ctx.session, values["sku"], product.id)
    if "status" in values:
        values["status"] = ProductStatus(values["status"])
    for key, value in values.items():
        setattr(product, key, value)
    product.updated_at = func.now()
    await ctx.session.flush()
    await ctx.session.refresh(product)
    changes = diff(before, snapshot(product, PRODUCT_FIELDS))
    if changes:
        ctx.audit("product.update", "product", product.id, diff=changes)
        await mark_product_changed(ctx, product.id, [await product_slugs(ctx.session, product.id)])
    return product


async def archive_product(ctx: MutationContext, product: Product) -> Product:
    return await update_product(ctx, product, ProductUpdate(status="archived"))


# ======================================================================================
# Translations
# ======================================================================================


async def list_translations(session: AsyncSession, product: Product) -> list[ProductTranslation]:
    return await _translations(session, product.id)


async def put_translation(
    ctx: MutationContext, product: Product, locale: str, data: TranslationIn
) -> tuple[ProductTranslation, bool]:
    ctx.principal.require_catalog_write()
    values = _translation_values(data)
    await _check_slug_free(ctx.session, product.id, locale, values["slug"])
    slugs_before = await product_slugs(ctx.session, product.id)
    tr = await ctx.session.get(ProductTranslation, (product.id, locale))
    created = tr is None
    before = None if tr is None else snapshot(tr, TRANSLATION_FIELDS)
    if tr is None:
        tr = ProductTranslation(product_id=product.id, locale=locale)
        ctx.session.add(tr)
    for key, value in values.items():
        setattr(tr, key, value)
    if not created:
        tr.updated_at = func.now()
    await ctx.session.flush()
    await ctx.session.refresh(tr)
    changes = diff(before, snapshot(tr, TRANSLATION_FIELDS))
    if changes:
        ctx.audit(
            "translation.create" if created else "translation.update",
            "product_translation",
            f"{product.id}:{locale}",
            diff=changes,
        )
        await mark_product_changed(
            ctx, product.id, [slugs_before, await product_slugs(ctx.session, product.id)]
        )
    return tr, created


async def delete_translation(ctx: MutationContext, product: Product, locale: str) -> None:
    """Refused while a website shows the product with `locale` as its default locale
    (the product would become untranslated there)."""
    ctx.principal.require_catalog_write()
    tr = await ctx.session.get(ProductTranslation, (product.id, locale))
    if tr is None:
        raise NotFound(f"No {locale!r} translation", code="translation_not_found")
    blocking = await ctx.session.scalars(
        select(Website.key)
        .join(WebsiteProduct, WebsiteProduct.website_id == Website.id)
        .where(
            WebsiteProduct.product_id == product.id,
            WebsiteProduct.is_visible.is_(True),
            Website.default_locale == locale,
        )
        .order_by(Website.key)
    )
    if keys := list(blocking):
        raise Conflict(
            f"The product is visible on websites whose default locale is {locale!r}",
            code="translation_required",
            extra={"websites": keys},
        )
    slugs_before = await product_slugs(ctx.session, product.id)
    ctx.audit(
        "translation.delete",
        "product_translation",
        f"{product.id}:{locale}",
        diff=diff(snapshot(tr, TRANSLATION_FIELDS), None),
    )
    await ctx.session.delete(tr)
    await ctx.session.flush()
    await mark_product_changed(ctx, product.id, [slugs_before])


# ======================================================================================
# Site assignments
# ======================================================================================


async def list_assignments(session: AsyncSession, website: Website) -> list[AssignmentOut]:
    rows = await session.execute(
        select(WebsiteProduct, Product, ProductTranslation.name)
        .join(Product, Product.id == WebsiteProduct.product_id)
        .outerjoin(
            ProductTranslation,
            (ProductTranslation.product_id == Product.id)
            & (ProductTranslation.locale == website.default_locale),
        )
        .where(WebsiteProduct.website_id == website.id)
        .order_by(WebsiteProduct.position, WebsiteProduct.product_id)
    )
    return [
        AssignmentOut(
            product_id=p.id,
            sku=p.sku,
            status=p.status.value,
            name=name,
            has_default_translation=name is not None,
            position=wp.position,
            is_visible=wp.is_visible,
            is_featured=wp.is_featured,
        )
        for wp, p, name in rows
    ]


async def _require_default_translation(
    session: AsyncSession, website: Website, product_ids: Sequence[uuid.UUID]
) -> None:
    translated = set(
        await session.scalars(
            select(ProductTranslation.product_id).where(
                ProductTranslation.product_id.in_(list(product_ids)),
                ProductTranslation.locale == website.default_locale,
            )
        )
    )
    missing = [pid for pid in product_ids if pid not in translated]
    if missing:
        raise ValidationFailed(
            f"Products need a {website.default_locale!r} translation to be visible on this site",
            code="default_translation_missing",
            extra={"product_ids": [str(pid) for pid in missing]},
        )


async def _next_position(session: AsyncSession, website_id: uuid.UUID) -> int:
    current = await session.scalar(
        select(func.max(WebsiteProduct.position)).where(WebsiteProduct.website_id == website_id)
    )
    return (current or 0) + POSITION_STEP


async def put_assignment(
    ctx: MutationContext, website: Website, product: Product, data: AssignmentIn
) -> tuple[WebsiteProduct, bool]:
    ctx.principal.require_site_edit(website.id)
    link = await ctx.session.get(WebsiteProduct, (website.id, product.id))
    created = link is None
    before = None if link is None else snapshot(link, ASSIGNMENT_FIELDS)
    was_featured = bool(link and link.is_featured)
    if link is None:
        link = WebsiteProduct(
            website_id=website.id,
            product_id=product.id,
            position=await _next_position(ctx.session, website.id),
            is_visible=True,
            is_featured=False,
        )
        ctx.session.add(link)
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is None:
            raise ValidationFailed(f"`{key}` cannot be null")
        setattr(link, key, value)
    if link.is_visible:
        await _require_default_translation(ctx.session, website, [product.id])
    await ctx.session.flush()
    await ctx.session.refresh(link)
    changes = diff(before, snapshot(link, ASSIGNMENT_FIELDS))
    if changes:
        ctx.audit(
            "assignment.create" if created else "assignment.update",
            "website_product",
            f"{website.id}:{product.id}",
            website_id=website.id,
            diff=changes,
        )
        ctx.changed(
            website.id,
            impact.product(
                SiteRef.of(website),
                product.id,
                await product_slugs(ctx.session, product.id),
                featured=was_featured or link.is_featured,
            ),
        )
    return link, created


async def delete_assignment(ctx: MutationContext, website: Website, product: Product) -> None:
    ctx.principal.require_site_edit(website.id)
    link = await ctx.session.get(WebsiteProduct, (website.id, product.id))
    if link is None:
        raise NotFound("Product is not assigned to this website", code="product_not_assigned")
    ctx.audit(
        "assignment.delete",
        "website_product",
        f"{website.id}:{product.id}",
        website_id=website.id,
        diff=diff(snapshot(link, ASSIGNMENT_FIELDS), None),
    )
    ctx.changed(
        website.id,
        impact.product(
            SiteRef.of(website),
            product.id,
            await product_slugs(ctx.session, product.id),
            featured=link.is_featured,
        ),
    )
    await ctx.session.delete(link)
    await ctx.session.flush()


async def reorder(ctx: MutationContext, website: Website, product_ids: Sequence[uuid.UUID]) -> None:
    """Set positions 10, 20, … in the given order. Products not listed keep their current
    positions, so send the complete list for a full reorder."""
    ctx.principal.require_site_edit(website.id)
    if len(set(product_ids)) != len(product_ids):
        raise ValidationFailed("product_ids must be unique")
    links = {
        link.product_id: link
        for link in await ctx.session.scalars(
            select(WebsiteProduct).where(
                WebsiteProduct.website_id == website.id,
                WebsiteProduct.product_id.in_(list(product_ids)),
            )
        )
    }
    missing = [str(pid) for pid in product_ids if pid not in links]
    if missing:
        raise ValidationFailed(
            "Some products are not assigned to this website",
            code="product_not_assigned",
            extra={"product_ids": missing},
        )
    for index, pid in enumerate(product_ids, start=1):
        links[pid].position = index * POSITION_STEP
    await ctx.session.flush()
    ctx.audit(
        "assignment.reorder",
        "website",
        website.id,
        website_id=website.id,
        diff={"order": [None, [str(pid) for pid in product_ids]]},
    )
    site = SiteRef.of(website)
    ctx.changed(
        website.id, impact.listing(site, site.locales).merge(impact.home(site, site.locales))
    )


async def bulk_assign(
    ctx: MutationContext, website: Website, product_ids: Sequence[uuid.UUID], *, is_visible: bool
) -> BulkAssignOut:
    ctx.principal.require_site_edit(website.id)
    unique_ids = list(dict.fromkeys(product_ids))
    found = set(await ctx.session.scalars(select(Product.id).where(Product.id.in_(unique_ids))))
    if unknown := [str(pid) for pid in unique_ids if pid not in found]:
        raise NotFound("Unknown products", code="product_not_found", extra={"product_ids": unknown})
    already = set(
        await ctx.session.scalars(
            select(WebsiteProduct.product_id).where(
                WebsiteProduct.website_id == website.id,
                WebsiteProduct.product_id.in_(unique_ids),
            )
        )
    )
    new_ids = [pid for pid in unique_ids if pid not in already]
    if new_ids:
        if is_visible:
            await _require_default_translation(ctx.session, website, new_ids)
        position = await _next_position(ctx.session, website.id)
        ctx.session.add_all(
            WebsiteProduct(
                website_id=website.id,
                product_id=pid,
                position=position + i * POSITION_STEP,
                is_visible=is_visible,
            )
            for i, pid in enumerate(new_ids)
        )
        await ctx.session.flush()
        ctx.audit(
            "assignment.bulk_create",
            "website",
            website.id,
            website_id=website.id,
            diff={"product_ids": [None, [str(pid) for pid in new_ids]], "is_visible": is_visible},
        )
        site = SiteRef.of(website)
        change = impact.listing(site, site.locales)
        slugs: dict[uuid.UUID, dict[str, str]] = {pid: {} for pid in new_ids}
        for pid, loc, slug in await ctx.session.execute(
            select(
                ProductTranslation.product_id, ProductTranslation.locale, ProductTranslation.slug
            ).where(ProductTranslation.product_id.in_(new_ids))
        ):
            slugs[pid][loc] = slug
        for pid in new_ids:
            change.merge(impact.product(site, pid, slugs[pid], featured=False))
        ctx.changed(website.id, change)
    return BulkAssignOut(
        assigned=new_ids, already_assigned=[pid for pid in unique_ids if pid in already]
    )
