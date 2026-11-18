"""Phase 7 acceptance: a new website goes live with zero code changes.

Follows docs/runbooks/add-website.md step by step, using only the admin API, and checks the
result on the public API the frontend consumes.
"""

import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.websites.schemas import DEFAULT_THEME
from tests.admin.conftest import PUBLIC, FakeTxtResolver, ok, pending_jobs

SITE = f"{PUBLIC}/sites/casa-verde-it"


async def test_new_site_live_without_code_changes(
    admin: httpx.AsyncClient, dns: FakeTxtResolver, db_session: AsyncSession
) -> None:
    # 1. Website (draft) ------------------------------------------------------------------
    site = await ok(
        await admin.post(
            "/websites",
            json={
                "key": "casa-verde-it",
                "name": "Casa Verde",
                "country_code": "IT",
                "default_locale": "it",
                "locales": ["it", "en"],
            },
        ),
        201,
    )
    sid = site["id"]

    # 2. Theme ----------------------------------------------------------------------------
    theme = {**DEFAULT_THEME, "radius": "lg", "product_card_style": "elevated"}
    await ok(
        await admin.put(
            f"/websites/{sid}/theme", json={"tokens": theme}, headers={"If-Match": '"1"'}
        )
    )

    # 3. Domain + DNS verification --------------------------------------------------------
    domain = await ok(
        await admin.post(
            f"/websites/{sid}/domains",
            json={"hostname": "casaverde.it", "cloudflare_zone_id": "zone-casaverde"},
        ),
        201,
    )
    dns.records[domain["verification"]["record_name"]] = [domain["verification"]["value"]]
    await ok(await admin.post(f"/domains/{domain['id']}/verify"))

    # 4. Content + SEO per locale ---------------------------------------------------------
    for locale, title in (("it", "Arredi naturali"), ("en", "Natural furniture")):
        await ok(
            await admin.put(
                f"/websites/{sid}/content/home/{locale}",
                json={
                    "title": title,
                    "blocks": [
                        {"type": "hero", "heading": title},
                        {"type": "featured_products", "limit": 4},
                    ],
                },
            ),
            201,
        )
        await ok(
            await admin.put(
                f"/websites/{sid}/seo/home/{locale}",
                json={"title": f"Casa Verde | {title}", "description": title},
            )
        )

    # 5. Products: a new one plus an existing catalog product ----------------------------
    product = await ok(
        await admin.post(
            "/products",
            json={
                "sku": "CV-SOFA-LINEN",
                "translations": {
                    "it": {"name": "Divano in lino"},
                    "en": {"name": "Linen sofa"},
                },
            },
        ),
        201,
    )
    await ok(
        await admin.put(
            f"/websites/{sid}/products/{product['id']}",
            json={"is_visible": True, "is_featured": True, "position": 1},
        )
    )

    # 6. Go live --------------------------------------------------------------------------
    enabled = await ok(await admin.post(f"/websites/{sid}/enable"))
    assert (enabled["status"], enabled["primary_host"]) == ("active", "casaverde.it")

    # The frontend can now discover and render the site.
    resolved = await ok(await admin.get(f"{PUBLIC}/resolve?host=casaverde.it"))
    assert resolved["website_key"] == "casa-verde-it"
    sites = await ok(await admin.get(f"{PUBLIC}/sites"))
    assert "casa-verde-it" in {s["key"] for s in sites["sites"]}

    bundle = await ok(await admin.get(f"{SITE}/bundle"))
    assert bundle["config"]["locale"] == "it"
    assert bundle["config"]["theme"]["tokens"]["radius"] == "lg"
    assert bundle["home"]["title"] == "Arredi naturali"
    assert [p["sku"] for p in bundle["home"]["featured_products"]] == ["CV-SOFA-LINEN"]
    assert bundle["slugs"] == ["divano-in-lino"]
    assert bundle["seo"]["home"]["title"] == "Casa Verde | Arredi naturali"

    detail = await ok(await admin.get(f"{SITE}/products/linen-sofa?locale=en"))
    assert detail["name"] == "Linen sofa"

    sitemap = await ok(await admin.get(f"{SITE}/sitemap"))
    assert {e["loc"] for e in sitemap["entries"]} == {
        "https://casaverde.it/",
        "https://casaverde.it/products",
        "https://casaverde.it/products/divano-in-lino",
        "https://casaverde.it/en",
        "https://casaverde.it/en/products",
        "https://casaverde.it/en/products/linen-sofa",
    }

    # The frontend is told to build it.
    job = (await pending_jobs(db_session))[uuid.UUID(sid)]
    assert job.full_site
