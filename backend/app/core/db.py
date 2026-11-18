"""Async database engine, session factory and the FastAPI session dependency.

Transaction ownership: services decide when to commit. The dependency yields a session and
closes it afterwards; anything not committed is rolled back on close.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.models  # noqa: F401  # register every mapper before any ORM query runs
from app.core.config import Settings


class Database:
    def __init__(self, settings: Settings, *, application_name: str = "showcase-api") -> None:
        server_settings = {"application_name": application_name, "timezone": "UTC"}
        if settings.db_statement_timeout_ms:
            server_settings["statement_timeout"] = str(settings.db_statement_timeout_ms)

        self.engine: AsyncEngine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_pre_ping=True,
            pool_recycle=1800,
            echo=False,  # SQL logging is controlled via the logging config (DB_ECHO)
            connect_args={"server_settings": server_settings},
        )
        self.sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False, autoflush=False
        )

    async def ping(self) -> None:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


async def get_session(db: Annotated[Database, Depends(get_db)]) -> AsyncIterator[AsyncSession]:
    async with db.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
DatabaseDep = Annotated[Database, Depends(get_db)]
