import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.security import verify_password
from app.models import (
    AdminRole,
    AdminUserWebsite,
    Base,
    Product,
    ProductTranslation,
    SeoMetadata,
    Website,
    WebsiteContent,
    WebsiteDomain,
    WebsiteProduct,
    WebsiteTheme,
)
from app.modules.auth.service import create_admin_user
from app.modules.content.schemas import content_blocks_adapter
from app.modules.websites.schemas import ThemeTokens
from app.seed import seed

COUNTED: list[type[Base]] = [
    Website,
    WebsiteDomain,
    WebsiteTheme,
    Product,
    ProductTranslation,
    WebsiteProduct,
    WebsiteContent,
    SeoMetadata,
]


async def _counts(session: AsyncSession) -> dict[str, int]:
    return {
        m.__tablename__: await session.scalar(select(func.count()).select_from(m)) or 0
        for m in COUNTED
    }


async def test_seed_is_idempotent(db_session: AsyncSession) -> None:
    await seed(db_session)
    first = await _counts(db_session)
    await seed(db_session)
    assert await _counts(db_session) == first


async def test_seed_shape(db_session: AsyncSession) -> None:
    summary = await seed(db_session)
    assert summary.websites == 2
    assert summary.products == 10
    locales = await db_session.scalars(select(ProductTranslation.locale).distinct())
    assert set(locales) == {"de", "en", "fr"}

    # Stored JSON validates against the API schemas.
    for tokens in await db_session.scalars(select(WebsiteTheme.tokens)):
        ThemeTokens.model_validate(tokens)
    for blocks in await db_session.scalars(select(WebsiteContent.blocks)):
        content_blocks_adapter.validate_python(blocks)

    # Products are shared: some are assigned to both sites.
    shared = await db_session.scalar(
        select(func.count()).select_from(
            select(WebsiteProduct.product_id)
            .group_by(WebsiteProduct.product_id)
            .having(func.count() > 1)
            .subquery()
        )
    )
    assert shared is not None
    assert shared > 0


async def test_create_admin(db_session: AsyncSession) -> None:
    user, created = await create_admin_user(
        db_session, email="  Boss@Example.COM ", password="a-long-password", role=AdminRole.ADMIN
    )
    assert created
    assert user.email == "boss@example.com"
    assert verify_password(user.password_hash, "a-long-password")

    with pytest.raises(Conflict):
        await create_admin_user(
            db_session,
            email="boss@example.com",
            password="another-password",
            role=AdminRole.ADMIN,
        )

    again, created = await create_admin_user(
        db_session,
        email="boss@example.com",
        password="another-password",
        role=AdminRole.ADMIN,
        reset_password_if_exists=True,
    )
    assert not created
    assert again.id == user.id
    assert verify_password(again.password_hash, "another-password")


async def test_site_editor_grants(db_session: AsyncSession) -> None:
    await seed(db_session)
    user, _ = await create_admin_user(
        db_session,
        email="editor@example.com",
        password="a-long-password",
        role=AdminRole.SITE_EDITOR,
        website_keys=["nordic-home-de"],
    )
    grants = await db_session.scalars(
        select(AdminUserWebsite).where(AdminUserWebsite.admin_user_id == user.id)
    )
    assert len(grants.all()) == 1


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"email": "no-at-sign", "password": "a-long-password"}, ValidationFailed),
        ({"email": "a@example.com", "password": "short"}, ValidationFailed),
        (
            {
                "email": "a@example.com",
                "password": "a-long-password",
                "role": AdminRole.SITE_EDITOR,
            },
            ValidationFailed,
        ),
        (
            {
                "email": "a@example.com",
                "password": "a-long-password",
                "role": AdminRole.SITE_EDITOR,
                "website_keys": ["missing-site"],
            },
            NotFound,
        ),
    ],
)
async def test_create_admin_validation(
    db_session: AsyncSession, kwargs: dict[str, object], error: type[Exception]
) -> None:
    kwargs.setdefault("role", AdminRole.ADMIN)
    with pytest.raises(error):
        await create_admin_user(db_session, **kwargs)  # type: ignore[arg-type]
