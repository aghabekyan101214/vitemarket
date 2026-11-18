"""Public API: tenant resolution, localization/fallback, isolation, SEO, caching."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.public import get_public_session
from app.models import (
    Product,
    ProductImage,
    ProductStatus,
    Website,
    WebsiteProduct,
    WebsiteStatus,
)
from app.seed import SeedSummary, seed
from tests.factories import make_product

DE = "/public/v1/sites/nordic-home-de"
FR = "/public/v1/sites/nordic-home-fr"


@pytest.fixture
async def seeded(db_session: AsyncSession) -> SeedSummary:
    return await seed(db_session)


@pytest.fixture
async def api(
    app: FastAPI, db_session: AsyncSession, seeded: SeedSummary
) -> AsyncIterator[httpx.AsyncClient]:
    """Client whose public endpoints read through the test's (rolled back) session."""

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_public_session] = _session
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


async def _json(client: httpx.AsyncClient, url: str, status: int = 200) -> Any:
    r = await client.get(url)
    assert r.status_code == status, r.text
    return r.json()


async def _product_id(session: AsyncSession, sku: str) -> Any:
    return await session.scalar(select(Product.id).where(Product.sku == sku))


# --- routing --------------------------------------------------------------------------


async def test_sites_lists_active_sites_and_hosts(api: httpx.AsyncClient) -> None:
    body = await _json(api, "/public/v1/sites")
    by_key = {site["key"]: site for site in body["sites"]}
    assert set(by_key) == {"nordic-home-de", "nordic-home-fr"}
    de = by_key["nordic-home-de"]
    assert de["primary_host"] == "nordic-home-de.localhost"
    # Pending domains are not routable.
    assert [h["hostname"] for h in de["hosts"]] == [
        "nordic-home-de.localhost",
        "www.nordic-home-de.localhost",
    ]


async def test_sites_hides_inactive_sites(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await db_session.execute(
        update(Website).where(Website.key == "nordic-home-fr").values(status=WebsiteStatus.DRAFT)
    )
    body = await _json(api, "/public/v1/sites")
    assert [site["key"] for site in body["sites"]] == ["nordic-home-de"]
    await _json(api, f"{FR}/config", 404)


@pytest.mark.parametrize(
    ("host", "redirect"),
    [
        ("nordic-home-de.localhost", None),
        ("NORDIC-HOME-DE.localhost:3000", None),
        ("www.nordic-home-de.localhost", "https://nordic-home-de.localhost"),
    ],
)
async def test_resolve(api: httpx.AsyncClient, host: str, redirect: str | None) -> None:
    body = await _json(api, f"/public/v1/resolve?host={host}")
    assert body["website_key"] == "nordic-home-de"
    assert body["locale"] == "de"
    assert body["locales"] == ["de", "en"]
    assert body["redirect"] == redirect


@pytest.mark.parametrize(
    "host",
    ["staging.nordic-home-de.localhost", "unknown.example.com", "127.0.0.1", "bad host"],
)
async def test_resolve_unknown_or_inactive(api: httpx.AsyncClient, host: str) -> None:
    body = await _json(api, f"/public/v1/resolve?host={host}", 404)
    assert body["code"] == "domain_not_found"


# --- config & locale handling ---------------------------------------------------------


async def test_config(api: httpx.AsyncClient) -> None:
    body = await _json(api, f"{DE}/config?locale=en")
    assert body["locale"] == "en"
    assert body["site"]["base_url"] == "https://nordic-home-de.localhost"
    assert body["theme"]["tokens"]["colors"]["primary"] == "#1F3A5F"
    assert body["nav"][1] == {"page": "products", "label": "Products", "path": "/en/products"}
    assert body["locale_links"] == [{"locale": "de", "path": "/"}, {"locale": "en", "path": "/en"}]
    assert body["media"]["transform_provider"] == "none"


async def test_default_locale_when_omitted(api: httpx.AsyncClient) -> None:
    body = await _json(api, f"{DE}/config")
    assert body["locale"] == "de"
    assert body["nav"][1]["label"] == "Produkte"


async def test_locale_errors(api: httpx.AsyncClient) -> None:
    body = await _json(api, f"{DE}/home?locale=fr", 404)
    assert body["code"] == "locale_not_supported"
    assert body["locales"] == ["de", "en"]
    body = await _json(api, f"{DE}/home?locale=xx_yy_zz", 422)
    assert body["code"] == "invalid_locale"
    await _json(api, "/public/v1/sites/missing-site/home", 404)
    await _json(api, "/public/v1/sites/Bad_Key!/home", 422)


# --- home & products ------------------------------------------------------------------


async def test_home(api: httpx.AsyncClient) -> None:
    body = await _json(api, f"{DE}/home?locale=en")
    assert body["locale"] == "en"
    assert [b["type"] for b in body["blocks"]] == ["hero", "featured_products", "features"]
    # Internal links are localized for the page's locale.
    assert body["blocks"][0]["cta"]["href"] == "/en/products"
    assert [p["sku"] for p in body["featured_products"]] == [
        "NH-CHAIR-OAK",
        "NH-TABLE-ASH",
        "NH-LAMP-LINEN",
    ]
    de = await _json(api, f"{DE}/home")
    assert de["blocks"][0]["cta"]["href"] == "/products"


async def test_products_pagination(api: httpx.AsyncClient) -> None:
    body = await _json(api, f"{DE}/products?locale=en&page=3&page_size=3")
    assert (body["total"], body["total_pages"], len(body["items"])) == (8, 3, 2)
    assert body["content"]["title"] == "Products"
    first = (await _json(api, f"{DE}/products?page_size=1"))["items"][0]
    assert first["path"] == "/products/eichen-loungesessel"
    past_end = await _json(api, f"{DE}/products?page=9&page_size=3")
    assert (past_end["items"], past_end["total"]) == ([], 8)
    await _json(api, f"{DE}/products?page_size=101", 422)


async def test_hidden_and_archived_products_are_excluded(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    body = await _json(api, f"{FR}/products?page_size=100")
    assert body["total"] == 7  # NH-VASE-CERAMIC is assigned but hidden
    assert "NH-VASE-CERAMIC" not in {p["sku"] for p in body["items"]}
    await _json(api, f"{FR}/products/vase-en-ceramique", 404)

    await db_session.execute(
        update(Product).where(Product.sku == "NH-RUG-JUTE").values(status=ProductStatus.ARCHIVED)
    )
    body = await _json(api, f"{FR}/products?page_size=100")
    assert body["total"] == 6
    await _json(api, f"{FR}/products/tapis-en-jute", 404)


async def test_site_isolation(api: httpx.AsyncClient) -> None:
    """A product assigned only to the FR site is invisible through the DE site."""
    de = await _json(api, f"{DE}/products?locale=en&page_size=100")
    assert "NH-MIRROR-BRASS" not in {p["sku"] for p in de["items"]}
    await _json(api, f"{DE}/products/round-brass-mirror?locale=en", 404)
    await _json(api, f"{DE}/seo?page=product&slug=round-brass-mirror&locale=en", 404)
    await _json(api, f"{FR}/products/miroir-rond-en-laiton", 200)


async def test_locale_fallback(
    api: httpx.AsyncClient, db_session: AsyncSession, seeded: SeedSummary
) -> None:
    site_id = seeded.website_ids["nordic-home-de"]
    german_only = await make_product(db_session, translations={"de": "Nur Deutsch"})
    french_only = await make_product(db_session, translations={"fr": "Seulement français"})
    for pos, product in ((1, german_only), (2, french_only)):
        db_session.add(WebsiteProduct(website_id=site_id, product_id=product.id, position=pos))
    await db_session.flush()

    body = await _json(api, f"{DE}/products?locale=en&page_size=100")
    items = {p["sku"]: p for p in body["items"]}
    # Untranslated → shown in the default locale; no translation in the chain → omitted.
    fallback = items[german_only.sku]
    assert (fallback["locale"], fallback["name"]) == ("de", "Nur Deutsch")
    assert french_only.sku not in items
    assert body["total"] == 9

    detail = await _json(api, f"{DE}{fallback['path'].removeprefix('/en')}?locale=en")
    assert detail["path"] == fallback["path"]
    # Content is German, so the canonical page is the German one; no fake hreflang.
    assert detail["canonical_path"] == f"/products/{fallback['slug']}"
    assert detail["alternates"] == []


async def test_product_detail(api: httpx.AsyncClient) -> None:
    body = await _json(api, f"{DE}/products/oak-lounge-chair?locale=en")
    assert body["name"] == "Oak Lounge Chair"
    assert body["attributes"]["material"] == "oak"
    assert body["description"].startswith("The Oak Lounge Chair")
    assert {a["hreflang"]: a["href"] for a in body["alternates"]} == {
        "de-DE": "https://nordic-home-de.localhost/products/eichen-loungesessel",
        "en-DE": "https://nordic-home-de.localhost/en/products/oak-lounge-chair",
        "x-default": "https://nordic-home-de.localhost/products/eichen-loungesessel",
    }
    # The German slug on the English page resolves to the English translation; the
    # frontend redirects to `path`.
    via_de_slug = await _json(api, f"{DE}/products/eichen-loungesessel?locale=en")
    assert via_de_slug["path"] == "/en/products/oak-lounge-chair"
    # …but the English slug on the German page does not exist.
    await _json(api, f"{DE}/products/oak-lounge-chair", 404)


async def test_images(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    pid = await _product_id(db_session, "NH-CHAIR-OAK")
    for pos, key in ((2, "b"), (1, "a")):
        db_session.add(
            ProductImage(
                product_id=pid,
                storage_key=f"products/{pid}/{key}.webp",
                content_type="image/webp",
                width=1600,
                height=1200,
                size_bytes=1000,
                position=pos,
                alt={"de": "Sessel"} if key == "a" else {},
            )
        )
    await db_session.flush()

    listing = await _json(api, f"{DE}/products?locale=en&page_size=1")
    image = listing["items"][0]["image"]
    assert image["url"] == f"http://localhost:8333/media/products/{pid}/a.webp"
    assert image["alt"] == "Sessel"  # alt falls back to the default locale
    detail = await _json(api, f"{DE}/products/oak-lounge-chair?locale=en")
    assert [i["key"].rsplit("/", 1)[1] for i in detail["images"]] == ["a.webp", "b.webp"]
    assert detail["images"][1]["alt"] == "Oak Lounge Chair"  # no alt → product name
    seo = await _json(api, f"{DE}/seo?page=product&slug=oak-lounge-chair&locale=en")
    assert seo["og"]["image"] == image["url"]


# --- SEO ------------------------------------------------------------------------------


async def test_seo_home(api: httpx.AsyncClient) -> None:
    body = await _json(api, f"{DE}/seo?page=home&locale=en")
    assert body["title"] == "Nordic Home Deutschland | Scandinavian design for your home"
    assert body["canonical"] == "https://nordic-home-de.localhost/en"
    assert body["robots"] == "index,follow"
    assert body["og"]["locale"] == "en_DE"
    assert [a["hreflang"] for a in body["alternates"]] == ["de-DE", "en-DE", "x-default"]
    assert body["json_ld"][0]["@type"] == "WebSite"


async def test_seo_single_locale_site_has_no_alternates(api: httpx.AsyncClient) -> None:
    body = await _json(api, f"{FR}/seo?page=products")
    assert body["canonical"] == "https://nordic-home-fr.localhost/products"
    assert body["alternates"] == []


async def test_seo_validation(api: httpx.AsyncClient) -> None:
    assert (await _json(api, f"{DE}/seo?page=product", 422))["code"] == "slug_required"
    await _json(api, f"{DE}/seo?page=checkout", 422)
    await _json(api, f"{DE}/seo?page=product&slug=does-not-exist", 404)


# --- bundle & sitemap -----------------------------------------------------------------


async def test_bundle(api: httpx.AsyncClient) -> None:
    body = await _json(api, f"{DE}/bundle?locale=en")
    assert body["config"]["locale"] == "en"
    assert len(body["products"]) == 8
    assert body["slugs"] == [p["slug"] for p in body["products"]]
    assert set(body["seo"]["product"]) == set(body["slugs"])
    assert [p["sku"] for p in body["home"]["featured_products"]] == [
        "NH-CHAIR-OAK",
        "NH-TABLE-ASH",
        "NH-LAMP-LINEN",
    ]
    # Bundle pieces equal the individual endpoints.
    assert body["home"] == await _json(api, f"{DE}/home?locale=en")
    assert body["seo"]["product"]["oak-lounge-chair"] == await _json(
        api, f"{DE}/seo?page=product&slug=oak-lounge-chair&locale=en"
    )
    assert body["seo"]["products"] == await _json(api, f"{DE}/seo?page=products&locale=en")


async def test_sitemap(api: httpx.AsyncClient) -> None:
    body = await _json(api, f"{DE}/sitemap")
    entries = body["entries"]
    assert len(entries) == 2 * (2 + 8)  # 2 locales x (home + listing + 8 products)
    assert entries[0]["loc"] == "https://nordic-home-de.localhost/"
    assert all(e["lastmod"] for e in entries)
    en = await _json(api, f"{DE}/sitemap?locale=en")
    assert {e["locale"] for e in en["entries"]} == {"en"}
    assert len(en["entries"]) == 10
    fr = await _json(api, f"{FR}/sitemap")
    assert len(fr["entries"]) == 2 + 7


# --- HTTP caching ---------------------------------------------------------------------


async def test_etag_and_304(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    url = f"{DE}/products?locale=en"
    first = await api.get(url)
    etag = first.headers["etag"]
    assert etag.startswith('W/"')
    assert "s-maxage=300" in first.headers["cache-control"]

    cached = await api.get(url, headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["etag"] == etag

    # Different parameters → different representation.
    other = await api.get(f"{DE}/products?locale=de")
    assert other.headers["etag"] != etag

    # Any content change bumps content_version → the old ETag no longer matches.
    await db_session.execute(
        update(Website)
        .where(Website.key == "nordic-home-de")
        .values(content_version=Website.content_version + 1)
    )
    fresh = await api.get(url, headers={"If-None-Match": etag})
    assert fresh.status_code == 200
    assert fresh.headers["etag"] != etag


async def test_body_etag_for_routing(api: httpx.AsyncClient) -> None:
    first = await api.get("/public/v1/sites")
    again = await api.get("/public/v1/sites", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


async def test_real_public_session_is_read_only(client: httpx.AsyncClient) -> None:
    """Without the test override: the real REPEATABLE READ / read-only session works."""
    r = await client.get("/public/v1/sites")
    assert r.status_code == 200
    assert r.json() == {"sites": []}  # test DB is empty outside rolled-back tests


# --- query budget ---------------------------------------------------------------------


class _QueryCounter:
    def __init__(self, session: AsyncSession) -> None:
        self.engine = session.bind.engine.sync_engine  # type: ignore[union-attr]
        self.count = 0

    def _on_execute(self, *_: object) -> None:
        self.count += 1

    def __enter__(self) -> "_QueryCounter":
        event.listen(self.engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._on_execute)


@pytest.mark.parametrize(
    "url",
    [
        f"{DE}/bundle?locale=en",
        f"{DE}/products?locale=en&page_size=100",
        f"{DE}/home?locale=en",
        f"{DE}/sitemap",
    ],
)
async def test_query_count_does_not_grow_with_catalog(
    api: httpx.AsyncClient, db_session: AsyncSession, seeded: SeedSummary, url: str
) -> None:
    async def queries() -> int:
        db_session.expunge_all()
        with _QueryCounter(db_session) as counter:
            assert (await api.get(url)).status_code == 200
        return counter.count

    baseline = await queries()
    site_id = seeded.website_ids["nordic-home-de"]
    for i in range(15):
        product = await make_product(db_session, translations={"de": f"Extra {i}", "en": f"X {i}"})
        db_session.add(
            WebsiteProduct(
                website_id=site_id, product_id=product.id, position=100 + i, is_featured=True
            )
        )
        db_session.add(
            ProductImage(
                product_id=product.id,
                storage_key=f"products/{product.id}/main.webp",
                content_type="image/webp",
                width=10,
                height=10,
                size_bytes=10,
            )
        )
    await db_session.flush()
    assert await queries() == baseline
    assert baseline <= 7


async def test_not_modified_costs_one_query(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    url = f"{DE}/bundle?locale=en"
    etag = (await api.get(url)).headers["etag"]
    with _QueryCounter(db_session) as counter:
        assert (await api.get(url, headers={"If-None-Match": etag})).status_code == 304
    assert counter.count == 1  # the site lookup; no content queries


async def test_public_session_settings(app: FastAPI) -> None:
    sessions = get_public_session(app.state.db, app.state.settings)
    session = await anext(sessions)
    try:
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert await session.scalar(text("SHOW statement_timeout")) == "5s"
    finally:
        await sessions.aclose()  # type: ignore[attr-defined]
