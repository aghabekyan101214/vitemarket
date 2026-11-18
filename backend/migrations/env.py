"""Alembic environment (async, asyncpg). DB URL comes from app settings / DATABASE_URL."""

import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.models import Base

config = context.config
settings = get_settings()
configure_logging(settings)

target_metadata = Base.metadata

# Allow `alembic -x db_url=...` (used by the test suite to migrate the test database).
DB_URL = context.get_x_argument(as_dictionary=True).get("db_url") or os.environ.get(
    "ALEMBIC_DATABASE_URL", settings.database_url
)


def _configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        **kwargs,  # type: ignore[arg-type]
    )


def run_migrations_offline() -> None:
    _configure(url=DB_URL, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(DB_URL, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
