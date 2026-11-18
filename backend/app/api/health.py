import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.db import DatabaseDep
from app.core.errors import ServiceUnavailable
from app.core.logging import get_logger

router = APIRouter(prefix="/health", tags=["health"])
log = get_logger(__name__)


class HealthStatus(BaseModel):
    status: str
    version: str


@router.get("/live", response_model=HealthStatus, summary="Liveness probe")
async def live(request: Request) -> HealthStatus:
    """The process is up and serving requests. Does not touch dependencies."""
    return HealthStatus(status="ok", version=request.app.version)


@router.get(
    "/ready",
    response_model=HealthStatus,
    summary="Readiness probe",
    responses={503: {"description": "A dependency (database) is unavailable"}},
)
async def ready(request: Request, db: DatabaseDep) -> HealthStatus:
    """The service can handle traffic: the database answers within 2 seconds."""
    try:
        async with asyncio.timeout(2):
            await db.ping()
    except Exception as exc:
        log.warning("readiness_check_failed", error=repr(exc))
        raise ServiceUnavailable("Database is unavailable.") from exc
    return HealthStatus(status="ok", version=request.app.version)
