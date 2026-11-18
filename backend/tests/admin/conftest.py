"""Admin API fixtures: seeded data, one user per role, an authenticated client per role."""

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.public import get_public_session
from app.core.config import Settings
from app.core.db import get_session
from app.core.security import create_access_token
from app.core.storage import MemoryStorage
from app.models import AdminRole, AuditLog, PublishJob, Website
from app.modules.auth.service import create_admin_user
from app.seed import SeedSummary, seed

PASSWORD = "a-long-test-password"


@dataclass
class FakeTxtResolver:
    records: dict[str, list[str]] = field(default_factory=dict)

    async def txt(self, name: str) -> list[str]:
        return self.records.get(name, [])


@pytest.fixture
async def seeded(db_session: AsyncSession) -> SeedSummary:
    return await seed(db_session)


@pytest.fixture
def storage() -> MemoryStorage:
    return MemoryStorage()


@pytest.fixture
def dns() -> FakeTxtResolver:
    return FakeTxtResolver()


@pytest.fixture
async def users(db_session: AsyncSession, seeded: SeedSummary) -> dict[str, uuid.UUID]:
    out = {}
    for role, sites in (
        (AdminRole.ADMIN, []),
        (AdminRole.CATALOG_MANAGER, []),
        (AdminRole.SITE_EDITOR, ["nordic-home-de"]),
    ):
        user, _ = await create_admin_user(
            db_session,
            email=f"{role.value}@test.example",
            password=PASSWORD,
            role=role,
            website_keys=sites,
        )
        out[role.value] = user.id
    await db_session.flush()
    return out


Client = Callable[[str], httpx.AsyncClient]


@pytest.fixture
async def as_role(
    app: FastAPI,
    db_session: AsyncSession,
    settings: Settings,
    users: dict[str, uuid.UUID],
    dns: FakeTxtResolver,
    storage: MemoryStorage,
) -> AsyncIterator[Client]:
    """`as_role("site_editor")` → client authenticated as that role ("anon" = no token)."""

    async def _session() -> AsyncIterator[AsyncSession]:
        # Test setup done through db_session counts as committed before the request (it
        # stays inside the test's outer transaction); a failing request then rolls back
        # only its own work, like production.
        await db_session.commit()
        try:
            yield db_session
        except Exception:
            # Like production (request failed → nothing committed): roll back to the
            # savepoint so the next request in the test starts from a clean state.
            await db_session.rollback()
            raise

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_public_session] = _session
    app.state.txt_resolver = dns
    app.state.storage = storage
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    clients: list[httpx.AsyncClient] = []

    def make(role: str) -> httpx.AsyncClient:
        headers = {}
        if role != "anon":
            token = create_access_token(
                users[role], secret=settings.jwt_secret.get_secret_value(), ttl_minutes=5
            )
            headers["Authorization"] = f"Bearer {token.token}"
        client = httpx.AsyncClient(
            transport=transport, base_url="http://testserver/admin/v1", headers=headers
        )
        clients.append(client)
        return client

    yield make
    for c in clients:
        await c.aclose()
    app.dependency_overrides.clear()


@pytest.fixture
def admin(as_role: Client) -> httpx.AsyncClient:
    return as_role("admin")


@pytest.fixture
def site_ids(seeded: SeedSummary) -> dict[str, uuid.UUID]:
    return {"de": seeded.website_ids["nordic-home-de"], "fr": seeded.website_ids["nordic-home-fr"]}


PUBLIC = "http://testserver/public/v1"


async def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


async def pending_jobs(session: AsyncSession) -> dict[uuid.UUID, PublishJob]:
    rows = await session.scalars(
        select(PublishJob)
        .where(PublishJob.status == "pending")
        .execution_options(populate_existing=True)
    )
    return {job.website_id: job for job in rows}


async def content_version(session: AsyncSession, website_id: uuid.UUID) -> int:
    version = await session.scalar(
        select(Website.content_version)
        .where(Website.id == website_id)
        .execution_options(populate_existing=True)
    )
    assert version is not None
    return version


async def audit_actions(session: AsyncSession) -> list[str]:
    rows = await session.scalars(select(AuditLog.action).order_by(AuditLog.id))
    return list(rows)
