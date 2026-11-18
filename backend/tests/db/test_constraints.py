"""Database-level integrity rules (the last line of defence behind service validation)."""

from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import configure_mappers

from app.modules.catalog.models import Product, ProductTranslation, WebsiteProduct
from app.modules.content.models import PageType, SeoMetadata, WebsiteContent
from app.modules.publishing.models import JobStatus, PublishJob
from app.modules.websites.models import Website, WebsiteDomain
from tests.factories import make_domain, make_product, make_website


async def assert_rejected(session: AsyncSession, *objs: Any) -> None:
    """Adding these objects must violate a constraint; the test transaction stays usable."""
    with pytest.raises((IntegrityError, DBAPIError)):  # noqa: PT012
        async with session.begin_nested():
            session.add_all(objs)
            await session.flush()


def test_all_mappers_configure() -> None:
    configure_mappers()  # fails if any relationship target cannot be resolved


async def test_server_defaults(db_session: AsyncSession) -> None:
    site = await make_website(db_session)
    domain = await make_domain(db_session, site)
    await db_session.refresh(site)
    await db_session.refresh(domain)
    assert site.id.version == 7
    assert site.status.value == "draft"
    assert site.content_version == 1
    assert site.published_version == 0
    assert len(domain.verification_token) == 32
    assert domain.status.value == "active"


@pytest.mark.parametrize(
    "overrides",
    [
        {"key": "Bad Key"},
        {"key": "-leading-dash"},
        {"country_code": "de"},
        {"default_locale": "de_DE"},
        {"default_locale": "fr", "locales": ["de", "en"]},
        {"locales": []},
    ],
)
async def test_website_checks(db_session: AsyncSession, overrides: dict[str, Any]) -> None:
    values = {
        "key": "valid-key",
        "name": "X",
        "country_code": "DE",
        "default_locale": "de",
        "locales": ["de"],
        **overrides,
    }
    await assert_rejected(db_session, Website(**values))


async def test_website_key_unique(db_session: AsyncSession) -> None:
    await make_website(db_session, key="dup-key")
    await assert_rejected(
        db_session,
        Website(key="dup-key", name="x", country_code="DE", default_locale="de", locales=["de"]),
    )


async def test_one_primary_domain_per_website(db_session: AsyncSession) -> None:
    site = await make_website(db_session)
    await make_domain(db_session, site, is_primary=True)
    await make_domain(db_session, site, is_primary=False)  # aliases are fine
    await assert_rejected(
        db_session,
        WebsiteDomain(website_id=site.id, hostname="second.example.com", is_primary=True),
    )


async def test_hostname_unique_across_websites(db_session: AsyncSession) -> None:
    a = await make_website(db_session)
    b = await make_website(db_session)
    await make_domain(db_session, a, hostname="shared.example.com")
    await assert_rejected(db_session, WebsiteDomain(website_id=b.id, hostname="shared.example.com"))


@pytest.mark.parametrize(
    "hostname", ["Upper.example.com", "host.example.com:8080", "trailing.example.com."]
)
async def test_hostname_must_be_normalized(db_session: AsyncSession, hostname: str) -> None:
    site = await make_website(db_session)
    await assert_rejected(db_session, WebsiteDomain(website_id=site.id, hostname=hostname))


async def test_translation_slug_unique_per_locale(db_session: AsyncSession) -> None:
    p1 = await make_product(db_session)
    p2 = await make_product(db_session)
    db_session.add(ProductTranslation(product_id=p1.id, locale="de", name="A", slug="stuhl"))
    # Same slug in another locale is fine.
    db_session.add(ProductTranslation(product_id=p2.id, locale="fr", name="B", slug="stuhl"))
    await db_session.flush()
    await assert_rejected(
        db_session, ProductTranslation(product_id=p2.id, locale="de", name="C", slug="stuhl")
    )


@pytest.mark.parametrize(
    ("locale", "slug"), [("de", "Has Spaces"), ("de", "trailing-"), ("DE", "ok-slug")]
)
async def test_translation_format_checks(db_session: AsyncSession, locale: str, slug: str) -> None:
    product = await make_product(db_session)
    await assert_rejected(
        db_session, ProductTranslation(product_id=product.id, locale=locale, name="x", slug=slug)
    )


async def test_assigned_product_cannot_be_deleted(db_session: AsyncSession) -> None:
    site = await make_website(db_session)
    product = await make_product(db_session)
    db_session.add(WebsiteProduct(website_id=site.id, product_id=product.id))
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(delete(Product).where(Product.id == product.id))


async def test_deleting_website_cascades(db_session: AsyncSession) -> None:
    site = await make_website(db_session)
    await make_domain(db_session, site)
    product = await make_product(db_session, translations={"de": "Stuhl"})
    db_session.add(WebsiteProduct(website_id=site.id, product_id=product.id))
    await db_session.flush()

    await db_session.execute(delete(Website).where(Website.id == site.id))
    domains = await db_session.scalars(
        select(WebsiteDomain).where(WebsiteDomain.website_id == site.id)
    )
    links = await db_session.scalars(
        select(WebsiteProduct).where(WebsiteProduct.website_id == site.id)
    )
    assert domains.all() == []
    assert links.all() == []
    # The shared product survives.
    assert await db_session.get(Product, product.id) is not None


async def test_content_unique_per_page_and_locale(db_session: AsyncSession) -> None:
    site = await make_website(db_session)
    db_session.add(WebsiteContent(website_id=site.id, page=PageType.HOME, locale="de", title="a"))
    await db_session.flush()
    await assert_rejected(
        db_session,
        WebsiteContent(website_id=site.id, page=PageType.HOME, locale="de", title="b"),
    )


async def test_content_rejects_product_page(db_session: AsyncSession) -> None:
    site = await make_website(db_session)
    await assert_rejected(
        db_session,
        WebsiteContent(website_id=site.id, page=PageType.PRODUCT, locale="de", title="x"),
    )


async def test_seo_unique_treats_null_product_as_equal(db_session: AsyncSession) -> None:
    site = await make_website(db_session)
    db_session.add(SeoMetadata(website_id=site.id, locale="de", page=PageType.HOME))
    await db_session.flush()
    await assert_rejected(
        db_session, SeoMetadata(website_id=site.id, locale="de", page=PageType.HOME)
    )


async def test_seo_product_id_only_for_product_pages(db_session: AsyncSession) -> None:
    site = await make_website(db_session)
    product = await make_product(db_session)
    await assert_rejected(
        db_session, SeoMetadata(website_id=site.id, locale="de", page=PageType.PRODUCT)
    )
    await assert_rejected(
        db_session,
        SeoMetadata(website_id=site.id, locale="de", page=PageType.HOME, product_id=product.id),
    )


async def test_one_pending_publish_job_per_site(db_session: AsyncSession) -> None:
    site = await make_website(db_session)
    db_session.add(PublishJob(website_id=site.id, content_version=1))
    db_session.add(PublishJob(website_id=site.id, content_version=1, status=JobStatus.SUCCEEDED))
    await db_session.flush()
    await assert_rejected(db_session, PublishJob(website_id=site.id, content_version=2))
