"""Minimal async factories for DB tests. Each returns a flushed ORM object."""

import itertools
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import Product, ProductTranslation
from app.modules.websites.models import DomainStatus, Website, WebsiteDomain

_seq = itertools.count(1)


async def make_website(session: AsyncSession, **overrides: Any) -> Website:
    n = next(_seq)
    values: dict[str, Any] = {
        "key": f"site-{n}",
        "name": f"Site {n}",
        "country_code": "DE",
        "default_locale": "de",
        "locales": ["de", "en"],
    }
    values.update(overrides)
    website = Website(**values)
    session.add(website)
    await session.flush()
    return website


async def make_domain(session: AsyncSession, website: Website, **overrides: Any) -> WebsiteDomain:
    n = next(_seq)
    values: dict[str, Any] = {
        "website_id": website.id,
        "hostname": f"host-{n}.example.com",
        "status": DomainStatus.ACTIVE,
    }
    values.update(overrides)
    domain = WebsiteDomain(**values)
    session.add(domain)
    await session.flush()
    return domain


async def make_product(
    session: AsyncSession, *, translations: dict[str, str] | None = None, **overrides: Any
) -> Product:
    n = next(_seq)
    product = Product(**{"sku": f"SKU-{n}", **overrides})
    session.add(product)
    await session.flush()
    for locale, name in (translations or {}).items():
        session.add(
            ProductTranslation(
                product_id=product.id, locale=locale, name=name, slug=f"{locale}-product-{n}"
            )
        )
    await session.flush()
    return product
