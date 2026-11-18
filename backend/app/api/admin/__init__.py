"""Admin API (`/admin/v1`), Bearer JWT. Responses are never cached (`no-store`)."""

from fastapi import APIRouter, Depends

from app.api.admin import auth, catalog, content, media, users, websites
from app.api.admin.deps import no_store

router = APIRouter(prefix="/admin/v1", dependencies=[Depends(no_store)])
router.include_router(auth.router)
router.include_router(websites.router)
router.include_router(content.router)
router.include_router(catalog.products)
router.include_router(catalog.assignments)
router.include_router(media.router)
router.include_router(users.router)
