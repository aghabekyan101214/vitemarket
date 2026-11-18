import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

# Test defaults; values already in the environment (e.g. from docker compose) win, except the
# database URL, which always points at the dedicated test database.
_TEST_ENV = {
    "APP_ENV": "test",
    "LOG_LEVEL": "WARNING",
    "LOG_FORMAT": "console",
    "JWT_SECRET": "test-jwt-secret-for-the-test-suite-only",
    "STORAGE_ENDPOINT_URL": "http://localhost:8333",
    "STORAGE_ACCESS_KEY_ID": "local-access-key",
    "STORAGE_SECRET_ACCESS_KEY": "local-secret-key",
    "STORAGE_BUCKET": "media",
    "STORAGE_PUBLIC_BASE_URL": "http://localhost:8333/media",
}
for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)
os.environ["APP_ENV"] = "test"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://showcase:showcase@localhost:5432/showcase_test",
)

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent


def run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """Run Alembic in a subprocess (its env.py owns an event loop) against the test DB."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result


@pytest.fixture(scope="session", autouse=True)
def _migrated_test_db() -> None:
    run_alembic("upgrade", "head")


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
async def db_session(settings: Settings) -> AsyncIterator[AsyncSession]:
    """Session bound to an outer transaction that is rolled back after the test.

    Code under test may commit freely: with join_transaction_mode="create_savepoint" its
    commits/rollbacks only affect a SAVEPOINT, never the outer transaction.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        outer = await conn.begin()
        session = AsyncSession(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await outer.rollback()
    await engine.dispose()


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
