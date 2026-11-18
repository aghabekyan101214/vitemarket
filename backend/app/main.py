"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api import admin, health, public
from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from app.core.storage import S3Storage

log = get_logger(__name__)

OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness and readiness probes."},
    {
        "name": "public",
        "description": "Read-only, CDN-cacheable site content for static generation. "
        "Responses carry `ETag` + `Cache-Control`; send `If-None-Match` to get 304s.",
    },
]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.db = Database(settings)
        if not hasattr(app.state, "storage"):  # tests may inject a MemoryStorage
            app.state.storage = S3Storage(settings)
        log.info("startup", env=settings.app_env, version=settings.app_version)
        try:
            yield
        finally:
            await app.state.db.dispose()
            log.info("shutdown")

    app = FastAPI(
        title="Showcase API",
        version=settings.app_version,
        description=(
            "Multi-tenant backend for product showcase websites. "
            "`/public/v1` serves read-only, CDN-cacheable content for static site generation; "
            "`/admin/v1` manages websites, products and publishing."
        ),
        openapi_tags=OPENAPI_TAGS,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    # Middleware order: last added = outermost. RequestContext wraps everything so the
    # request id and access log cover CORS/GZip responses and unhandled errors too.
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_request_bytes,
        max_multipart_bytes=settings.upload_max_bytes + 1024 * 1024,  # file + form overhead
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "If-Match", "If-None-Match"],
            expose_headers=["ETag", "X-Request-ID"],
            max_age=600,
        )
    app.add_middleware(RequestContextMiddleware, hsts=settings.is_production)

    app.include_router(health.router)
    app.include_router(public.router)
    app.include_router(admin.router)
    return app


app = create_app()
