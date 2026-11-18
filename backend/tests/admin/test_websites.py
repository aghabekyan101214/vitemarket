import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WebsiteProduct
from tests.admin.conftest import (
    PUBLIC,
    FakeTxtResolver,
    audit_actions,
    content_version,
    ok,
    pending_jobs,
)
from tests.factories import make_product

NEW_SITE = {
    "key": "nordic-home-at",
    "name": "Nordic Home Österreich",
    "country_code": "at",
    "default_locale": "de",
    "locales": ["de", "EN"],
}


async def test_onboard_a_new_website(
    admin: httpx.AsyncClient, dns: FakeTxtResolver, db_session: AsyncSession
) -> None:
    """Create → add domain → verify → enable → visible on the public API."""
    site = await ok(await admin.post("/websites", json=NEW_SITE), 201)
    assert (site["status"], site["country_code"], site["locales"]) == ("draft", "AT", ["de", "en"])
    theme = await admin.get(f"/websites/{site['id']}/theme")
    assert theme.headers["etag"] == '"1"'

    enable = await admin.post(f"/websites/{site['id']}/enable")
    assert (enable.status_code, enable.json()["code"]) == (409, "primary_domain_required")

    domain = await ok(
        await admin.post(
            f"/websites/{site['id']}/domains", json={"hostname": "Shop.Example.AT:443"}
        ),
        201,
    )
    assert (domain["hostname"], domain["is_primary"], domain["status"]) == (
        "shop.example.at",
        True,  # first domain becomes primary
        "pending",
    )
    record = domain["verification"]
    assert record["record_name"] == "_showcase-verify.shop.example.at"

    failed = await admin.post(f"/domains/{domain['id']}/verify")
    assert (failed.status_code, failed.json()["code"]) == (412, "domain_verification_failed")

    dns.records[record["record_name"]] = ["unrelated", record["value"]]
    verified = await ok(await admin.post(f"/domains/{domain['id']}/verify"))
    assert verified["status"] == "active"
    assert verified["verified_at"]

    enabled = await ok(await admin.post(f"/websites/{site['id']}/enable"))
    assert enabled["status"] == "active"
    assert enabled["primary_host"] == "shop.example.at"

    resolved = await ok(await admin.get(f"{PUBLIC}/resolve?host=shop.example.at"))
    assert resolved["website_key"] == "nordic-home-at"

    assert await audit_actions(db_session) == [
        "website.create",
        "domain.create",
        "domain.verify",
        "website.enable",
    ]


async def test_website_validation(admin: httpx.AsyncClient, site_ids: dict[str, uuid.UUID]) -> None:
    bad = {**NEW_SITE, "default_locale": "fr"}
    assert (await admin.post("/websites", json=bad)).status_code == 422
    duplicate = await admin.post("/websites", json={**NEW_SITE, "key": "nordic-home-de"})
    assert duplicate.status_code == 409
    # key is immutable
    assert (await admin.patch(f"/websites/{site_ids['de']}", json={"key": "x"})).status_code == 422
    r = await admin.patch(f"/websites/{site_ids['de']}", json={"locales": ["en"]})
    assert r.json()["code"] == "invalid_locales"


async def test_default_locale_change_requires_translations(
    admin: httpx.AsyncClient, site_ids: dict[str, uuid.UUID], db_session: AsyncSession
) -> None:
    german = await make_product(db_session, translations={"de": "Nur Deutsch"})
    db_session.add(WebsiteProduct(website_id=site_ids["de"], product_id=german.id))
    await db_session.flush()
    sku = german.sku  # (the failed request rolls back the session, expiring objects)
    r = await admin.patch(f"/websites/{site_ids['de']}", json={"default_locale": "en"})
    assert (r.status_code, r.json()["code"]) == (409, "default_translation_missing")
    assert r.json()["skus"] == [sku]


async def test_settings_change_bumps_version_and_queues_full_publish(
    admin: httpx.AsyncClient, site_ids: dict[str, uuid.UUID], db_session: AsyncSession
) -> None:
    before = await content_version(db_session, site_ids["de"])
    await ok(await admin.patch(f"/websites/{site_ids['de']}", json={"name": "Renamed"}))
    assert await content_version(db_session, site_ids["de"]) == before + 1
    job = (await pending_jobs(db_session))[site_ids["de"]]
    assert job.full_site
    assert set(job.tags) == {f"site:{site_ids['de']}", "sites"}

    # No-op update: no new version.
    await ok(await admin.patch(f"/websites/{site_ids['de']}", json={"name": "Renamed"}))
    assert await content_version(db_session, site_ids["de"]) == before + 1


async def test_domain_rules(admin: httpx.AsyncClient, site_ids: dict[str, uuid.UUID]) -> None:
    domains = await ok(await admin.get(f"/websites/{site_ids['de']}/domains"))
    primary = next(d for d in domains if d["is_primary"])
    pending = next(d for d in domains if d["status"] == "pending")

    taken = await admin.post(
        f"/websites/{site_ids['fr']}/domains", json={"hostname": "nordic-home-de.localhost"}
    )
    assert (taken.status_code, taken.json()["code"]) == (409, "domain_exists")

    for r in (
        await admin.delete(f"/domains/{primary['id']}"),
        await admin.patch(f"/domains/{primary['id']}", json={"status": "disabled"}),
        await admin.post(f"/domains/{pending['id']}/make-primary"),
    ):
        assert r.status_code == 409, r.text

    www = next(d for d in domains if d["hostname"].startswith("www."))
    switched = await ok(await admin.post(f"/domains/{www['id']}/make-primary"))
    assert switched["is_primary"]
    domains = await ok(await admin.get(f"/websites/{site_ids['de']}/domains"))
    assert [d["hostname"] for d in domains if d["is_primary"]] == ["www.nordic-home-de.localhost"]
    # The old primary is now an alias and can be removed.
    assert (await admin.delete(f"/domains/{primary['id']}")).status_code == 204


async def test_force_verify(admin: httpx.AsyncClient, site_ids: dict[str, uuid.UUID]) -> None:
    domains = await ok(await admin.get(f"/websites/{site_ids['de']}/domains"))
    pending = next(d for d in domains if d["status"] == "pending")
    forced = await ok(await admin.post(f"/domains/{pending['id']}/verify?force=true"))
    assert forced["status"] == "active"


async def test_theme_optimistic_locking(
    admin: httpx.AsyncClient, site_ids: dict[str, uuid.UUID]
) -> None:
    url = f"/websites/{site_ids['de']}/theme"
    current = await admin.get(url)
    tokens = current.json()["tokens"]
    tokens["colors"]["primary"] = "#000000"

    assert (await admin.put(url, json={"tokens": tokens})).status_code == 428
    saved = await admin.put(
        url, json={"tokens": tokens}, headers={"If-Match": current.headers["etag"]}
    )
    assert saved.status_code == 200
    assert saved.headers["etag"] == '"2"'
    stale = await admin.put(url, json={"tokens": tokens}, headers={"If-Match": '"1"'})
    assert (stale.status_code, stale.json()["current_version"]) == (412, 2)
    invalid = {**tokens, "radius": "huge"}
    assert (
        await admin.put(url, json={"tokens": invalid}, headers={"If-Match": '"2"'})
    ).status_code == 422
