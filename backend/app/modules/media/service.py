"""Storing uploads and retiring objects.

Upload order: process → PutObject → DB row. If the DB step fails, the caller deletes the
object (compensation). Objects are never deleted inline: `retire()` schedules them for the
GC sweep after the retention period.
"""

import asyncio
import uuid
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.storage import IMMUTABLE_CACHE_CONTROL, Storage
from app.modules.media.images import ProcessedImage, process_image
from app.modules.media.models import DeletedMedia

log = get_logger(__name__)


def product_image_key(product_id: uuid.UUID, ext: str) -> str:
    return f"products/{product_id}/{uuid.uuid4().hex}.{ext}"


def theme_asset_key(website_id: uuid.UUID, kind: str, ext: str) -> str:
    return f"sites/{website_id}/{kind}/{uuid.uuid4().hex}.{ext}"


async def process_upload(data: bytes, settings: Settings) -> ProcessedImage:
    return await asyncio.to_thread(process_image, data, max_bytes=settings.upload_max_bytes)


async def store(storage: Storage, key: str, image: ProcessedImage) -> None:
    await storage.put(
        key, image.data, content_type=image.content_type, cache_control=IMMUTABLE_CACHE_CONTROL
    )


async def discard(storage: Storage, key: str) -> None:
    """Compensation after a failed DB write: the object was never referenced."""
    failed = await storage.delete([key])
    if failed:
        log.error("orphaned_object", key=key)


async def retire(session: AsyncSession, keys: Iterable[str | None], settings: Settings) -> None:
    """Schedule objects for deletion after the retention period (same transaction as the
    DB change that dropped the reference)."""
    rows = [{"storage_key": k} for k in keys if k]
    if not rows:
        return
    purge_after = func.now() + timedelta(days=settings.media_retention_days)
    await session.execute(
        pg_insert(DeletedMedia)
        .values([{**r, "purge_after": purge_after} for r in rows])
        .on_conflict_do_nothing(index_elements=[DeletedMedia.storage_key])
    )


async def purge_due(session: AsyncSession, storage: Storage, *, batch_size: int = 500) -> int:
    """Delete objects whose retention has passed. Safe with concurrent workers."""
    due = list(
        await session.scalars(
            select(DeletedMedia)
            .where(DeletedMedia.purge_after <= func.now())
            .order_by(DeletedMedia.purge_after)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    if not due:
        return 0
    failed = set(await storage.delete([m.storage_key for m in due]))
    done = [m.id for m in due if m.storage_key not in failed]
    if done:
        await session.execute(delete(DeletedMedia).where(DeletedMedia.id.in_(done)))
    if failed:
        retry = [m.id for m in due if m.storage_key in failed]
        await session.execute(
            update(DeletedMedia)
            .where(DeletedMedia.id.in_(retry))
            .values(
                attempts=DeletedMedia.attempts + 1,
                purge_after=func.now() + timedelta(hours=1),
            )
        )
        log.warning("media_gc_failures", count=len(failed))
    await session.commit()
    log.info("media_gc", deleted=len(done), failed=len(failed))
    return len(done)
