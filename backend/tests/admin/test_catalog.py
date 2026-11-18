import uuid

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product, Website
from tests.admin.conftest import PUBLIC, Client, content_version, ok, pending_jobs


async def _pid(session: AsyncSession, sku: str) -> uuid.UUID:
    pid = await session.scalar(select(Product.id).where(Product.sku == sku))
    assert pid is not None
    return pid


async def test_create_product(admin: httpx.AsyncClient, db_session: AsyncSession) -> None:
    body = await ok(
        await admin.post(
            "/products",
            json={
                "sku": "NH-DESK-ELM",
                "attributes": {"material": "elm"},
                "translations": {
                    "de": {"name": "Schreibtisch aus Ulme"},
                    "EN": {"name": "Elm Desk", "slug": "elm-writing-desk"},
                },
            },
        ),
        201,
    )
    slugs = {t["locale"]: t["slug"] for t in body["translations"]}
    assert slugs == {"de": "schreibtisch-aus-ulme", "en": "elm-writing-desk"}
    assert body["websites"] == []
    assert await pending_jobs(db_session) == {}  # not on any site yet

    dup = await admin.post("/products", json={"sku": "nh-desk-elm"})
    assert (dup.status_code, dup.json()["code"]) == (409, "sku_taken")
    taken = await admin.put(
        f"/products/{body['id']}/translations/fr", json={"name": "x", "slug": "tapis-en-jute"}
    )
    assert (taken.status_code, taken.json()["code"]) == (409, "slug_taken")


async def test_search_products(admin: httpx.AsyncClient, site_ids: dict[str, uuid.UUID]) -> None:
    by_name = await ok(await admin.get("/products?q=lounge"))
    assert [p["sku"] for p in by_name["items"]] == ["NH-CHAIR-OAK"]
    assert by_name["items"][0]["names"]["fr"] == "Fauteuil lounge en chêne"
    by_site = await ok(await admin.get(f"/products?website_id={site_ids['fr']}&page_size=5"))
    assert (by_site["total"], len(by_site["items"])) == (8, 5)
    assert (await ok(await admin.get("/products?status=archived")))["total"] == 0


async def test_shared_product_edit_publishes_every_site_using_it(
    as_role: Client, site_ids: dict[str, uuid.UUID], db_session: AsyncSession
) -> None:
    catalog = as_role("catalog_manager")
    lamp = await _pid(db_session, "NH-LAMP-LINEN")  # on both sites (featured on DE)
    chair = await _pid(db_session, "NH-CHAIR-OAK")  # DE only
    public_fr = f"{PUBLIC}/sites/nordic-home-fr/products/lampadaire-en-lin"
    fr_etag = (await catalog.get(public_fr)).headers["etag"]

    await ok(
        await catalog.put(f"/products/{lamp}/translations/fr", json={"name": "Lampadaire en lin"})
    )
    jobs = await pending_jobs(db_session)
    assert set(jobs) == {site_ids["de"], site_ids["fr"]}
    de_job, fr_job = jobs[site_ids["de"]], jobs[site_ids["fr"]]
    assert f"site:{site_ids['fr']}:product:{lamp}" in fr_job.tags
    assert f"site:{site_ids['de']}:home:de" in de_job.tags  # featured on DE
    assert f"site:{site_ids['fr']}:home:fr" not in fr_job.tags  # not featured on FR

    # Edited via the catalog, the FR site's public ETag changed.
    assert (await catalog.get(public_fr, headers={"If-None-Match": fr_etag})).status_code == 200

    # Debounce: a second edit merges into the same pending job.
    await ok(await catalog.patch(f"/products/{chair}", json={"attributes": {"material": "oak"}}))
    jobs_after = await pending_jobs(db_session)
    assert jobs_after[site_ids["de"]].id == de_job.id
    assert f"site:{site_ids['de']}:product:{chair}" in jobs_after[site_ids["de"]].tags
    assert f"site:{site_ids['de']}:product:{lamp}" in jobs_after[site_ids["de"]].tags
    assert jobs_after[site_ids["fr"]].tags == fr_job.tags


async def test_slug_change_purges_old_and_new_paths(
    admin: httpx.AsyncClient, site_ids: dict[str, uuid.UUID], db_session: AsyncSession
) -> None:
    chair = await _pid(db_session, "NH-CHAIR-OAK")
    await ok(
        await admin.put(
            f"/products/{chair}/translations/en", json={"name": "Oak Chair", "slug": "oak-chair"}
        )
    )
    paths = set((await pending_jobs(db_session))[site_ids["de"]].paths)
    assert {"/en/products/oak-lounge-chair", "/en/products/oak-chair"} <= paths


async def test_manual_publish_when_auto_publish_is_off(
    admin: httpx.AsyncClient, site_ids: dict[str, uuid.UUID], db_session: AsyncSession
) -> None:
    fr = site_ids["fr"]
    await db_session.execute(update(Website).where(Website.id == fr).values(auto_publish=False))
    version = await content_version(db_session, fr)
    rug = await _pid(db_session, "NH-RUG-JUTE")
    await ok(await admin.patch(f"/products/{rug}", json={"attributes": {"material": "jute"}}))
    assert await content_version(db_session, fr) == version + 1  # ETags change…
    assert fr not in await pending_jobs(db_session)  # …but nothing is published

    site = await ok(await admin.get(f"/websites/{fr}"))
    assert site["has_unpublished_changes"]
    job = await ok(await admin.post(f"/websites/{fr}/publish"), 202)
    assert (job["full_site"], job["tags"]) == (True, [f"site:{fr}"])
    listed = await ok(await admin.get(f"/websites/{fr}/publish-jobs"))
    assert [j["id"] for j in listed] == [job["id"]]


async def test_archive_hides_product_everywhere(
    admin: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    lamp = await _pid(db_session, "NH-LAMP-LINEN")
    await ok(await admin.post(f"/products/{lamp}/archive"))
    for site in ("nordic-home-de", "nordic-home-fr"):
        listing = await ok(await admin.get(f"{PUBLIC}/sites/{site}/products?page_size=100"))
        assert "NH-LAMP-LINEN" not in {p["sku"] for p in listing["items"]}


async def test_assignment_rules(
    as_role: Client, site_ids: dict[str, uuid.UUID], db_session: AsyncSession
) -> None:
    editor = as_role("site_editor")
    de = site_ids["de"]
    catalog = as_role("catalog_manager")
    created = await ok(
        await catalog.post(
            "/products", json={"sku": "ONLY-EN", "translations": {"en": {"name": "Only English"}}}
        ),
        201,
    )
    pid = created["id"]

    # Visible requires a translation in the site's default locale (de).
    r = await editor.put(f"/websites/{de}/products/{pid}", json={})
    assert (r.status_code, r.json()["code"]) == (422, "default_translation_missing")
    hidden = await ok(
        await editor.put(f"/websites/{de}/products/{pid}", json={"is_visible": False})
    )
    assert (hidden["is_visible"], hidden["has_default_translation"]) == (False, False)
    assert hidden["position"] == 90  # appended after the 8 seeded products

    await ok(
        await catalog.put(f"/products/{pid}/translations/de", json={"name": "Nur Englisch"}), 201
    )
    shown = await ok(await editor.put(f"/websites/{de}/products/{pid}", json={"is_visible": True}))
    assert shown["is_visible"]

    # The default-locale translation can no longer be removed.
    r = await catalog.delete(f"/products/{pid}/translations/de")
    assert (r.status_code, r.json()["code"]) == (409, "translation_required")
    assert r.json()["websites"] == ["nordic-home-de"]
    assert (await catalog.delete(f"/products/{pid}/translations/en")).status_code == 204

    assert (await editor.delete(f"/websites/{de}/products/{pid}")).status_code == 204
    assert (await editor.delete(f"/websites/{de}/products/{pid}")).status_code == 404


async def test_reorder_and_bulk_assign(
    as_role: Client, site_ids: dict[str, uuid.UUID], db_session: AsyncSession
) -> None:
    editor = as_role("site_editor")
    de = site_ids["de"]
    current = await ok(await editor.get(f"/websites/{de}/products"))
    reversed_ids = [a["product_id"] for a in reversed(current)]
    r = await editor.put(f"/websites/{de}/products/order", json={"product_ids": reversed_ids})
    assert r.status_code == 204
    listing = await ok(await editor.get(f"{PUBLIC}/sites/nordic-home-de/products?page_size=100"))
    assert [p["id"] for p in listing["items"]] == reversed_ids

    mirror = str(await _pid(db_session, "NH-MIRROR-BRASS"))
    unassigned = await editor.put(f"/websites/{de}/products/order", json={"product_ids": [mirror]})
    assert unassigned.status_code == 422

    result = await ok(
        await editor.post(
            f"/websites/{de}/products/bulk-assign",
            json={"product_ids": [mirror, reversed_ids[0]]},
        )
    )
    assert (result["assigned"], result["already_assigned"]) == ([mirror], [reversed_ids[0]])
    listing = await ok(await editor.get(f"{PUBLIC}/sites/nordic-home-de/products?page_size=100"))
    assert listing["items"][-1]["id"] == mirror
