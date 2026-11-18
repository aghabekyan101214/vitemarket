import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product, WebsiteContent
from tests.admin.conftest import PUBLIC, content_version, ok, pending_jobs

HOME = {
    "title": "Neu",
    "subtitle": "Untertitel",
    "blocks": [
        {"type": "hero", "heading": "Hallo", "cta": {"label": "Los", "href": "/products"}},
        {"type": "text", "body": '<p onclick="x()">Sicher <img src=x onerror=alert(1)></p>'},
    ],
}


async def test_content_editing_flow(
    admin: httpx.AsyncClient, site_ids: dict[str, uuid.UUID], db_session: AsyncSession
) -> None:
    de = site_ids["de"]
    url = f"/websites/{de}/content/home/de"
    public_url = f"{PUBLIC}/sites/nordic-home-de/home"
    public_etag = (await admin.get(public_url)).headers["etag"]

    current = await admin.get(url)
    assert current.headers["etag"] == '"1"'

    missing = await admin.put(url, json=HOME)
    assert (missing.status_code, missing.json()["code"]) == (428, "if_match_required")
    stale = await admin.put(url, json=HOME, headers={"If-Match": '"7"'})
    assert stale.status_code == 412

    saved = await admin.put(url, json=HOME, headers={"If-Match": current.headers["etag"]})
    body = await ok(saved)
    assert saved.headers["etag"] == '"2"'
    assert body["blocks"][1]["body"] == "<p>Sicher </p>"  # sanitized

    # Public API reflects it immediately with a new ETag; the old one no longer matches.
    fresh = await admin.get(public_url, headers={"If-None-Match": public_etag})
    assert fresh.status_code == 200
    assert fresh.json()["title"] == "Neu"

    job = (await pending_jobs(db_session))[de]
    assert job.tags == [f"site:{de}:home:de"]
    assert job.paths == ["/"]
    assert not job.full_site


async def test_create_content_for_new_locale_needs_no_if_match(
    admin: httpx.AsyncClient, site_ids: dict[str, uuid.UUID], db_session: AsyncSession
) -> None:
    row = await db_session.scalar(
        select(WebsiteContent).where(
            WebsiteContent.website_id == site_ids["de"],
            WebsiteContent.page == "products",
            WebsiteContent.locale == "en",
        )
    )
    await db_session.delete(row)
    await db_session.flush()
    created = await admin.put(
        f"/websites/{site_ids['de']}/content/products/en", json={"title": "All products"}
    )
    assert created.status_code == 201
    assert created.headers["etag"] == '"1"'
    # The products title is the nav label → the whole site is regenerated.
    assert (await pending_jobs(db_session))[site_ids["de"]].full_site


async def test_content_validation(admin: httpx.AsyncClient, site_ids: dict[str, uuid.UUID]) -> None:
    de = site_ids["de"]
    not_enabled = await admin.put(f"/websites/{de}/content/home/fr", json=HOME)
    assert (not_enabled.status_code, not_enabled.json()["code"]) == (422, "locale_not_enabled")
    assert (await admin.put(f"/websites/{de}/content/product/de", json=HOME)).status_code == 422
    unknown_block = {"title": "x", "blocks": [{"type": "carousel"}]}
    r = await admin.put(
        f"/websites/{de}/content/home/de", json=unknown_block, headers={"If-Match": '"1"'}
    )
    assert r.status_code == 422


async def test_seo_overrides(
    admin: httpx.AsyncClient, site_ids: dict[str, uuid.UUID], db_session: AsyncSession
) -> None:
    de = site_ids["de"]
    table = await db_session.scalar(select(Product.id).where(Product.sku == "NH-TABLE-ASH"))
    mirror = await db_session.scalar(select(Product.id).where(Product.sku == "NH-MIRROR-BRASS"))

    r = await admin.put(f"/websites/{de}/seo/product/en", json={"title": "x"})
    assert r.status_code == 422  # product_id required
    r = await admin.put(f"/websites/{de}/seo/home/en?product_id={table}", json={"title": "x"})
    assert r.status_code == 422  # product_id not allowed
    r = await admin.put(f"/websites/{de}/seo/product/en?product_id={mirror}", json={"title": "x"})
    assert (r.status_code, r.json()["code"]) == (404, "product_not_assigned")
    r = await admin.put(f"/websites/{de}/seo/home/en", json={"canonical_url": "http://x"})
    assert r.status_code == 422  # https only

    version = await content_version(db_session, de)
    await ok(
        await admin.put(
            f"/websites/{de}/seo/product/en?product_id={table}",
            json={"title": "Best table", "robots": "noindex,follow"},
        )
    )
    assert await content_version(db_session, de) == version + 1
    seo = await ok(
        await admin.get(
            f"{PUBLIC}/sites/nordic-home-de/seo?page=product&slug=ash-dining-table&locale=en"
        )
    )
    assert (seo["title"], seo["robots"]) == ("Best table", "noindex,follow")
    job = (await pending_jobs(db_session))[de]
    assert f"site:{de}:product:{table}" in job.tags
    assert "/en/products/ash-dining-table" in job.paths

    rows = await ok(await admin.get(f"/websites/{de}/seo?locale=en"))
    assert any(r["product_id"] == str(table) for r in rows)
    assert (
        await admin.delete(f"/websites/{de}/seo/product/en?product_id={table}")
    ).status_code == 204
    assert (
        await admin.delete(f"/websites/{de}/seo/product/en?product_id={table}")
    ).status_code == 404
