"""Background worker entrypoint: `python -m app.worker`.

Runs until SIGTERM:
- publish jobs: claim due jobs (SKIP LOCKED) → signed webhook → CDN purge → published;
- media GC: purge objects of deleted images after the retention period.

Any number of worker processes can run side by side.
"""

import asyncio
import contextlib
import signal
import time

import httpx

from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.logging import configure_logging, get_logger
from app.core.storage import S3Storage, Storage
from app.modules.media.service import purge_due
from app.modules.publishing.processor import Publisher, seconds_until_next

log = get_logger("app.worker")

POLL_INTERVAL_SECONDS = 5.0
MIN_SLEEP_SECONDS = 0.2


class Worker:
    def __init__(
        self, settings: Settings, db: Database, storage: Storage, publisher: Publisher
    ) -> None:
        self.settings = settings
        self.db = db
        self.storage = storage
        self.publisher = publisher
        self._next_gc = 0.0

    async def run_once(self) -> int:
        """One iteration of every task; returns how much work was done."""
        handled = await self.publisher.run_once()
        now = time.monotonic()
        if now >= self._next_gc:
            async with self.db.sessionmaker() as session:
                purged = await purge_due(session, self.storage)
            handled += purged
            # Keep sweeping while there is a backlog; otherwise wait for the next interval.
            if purged == 0:
                self._next_gc = now + self.settings.media_gc_interval_seconds
        return handled

    async def idle_seconds(self) -> float:
        """Sleep until the next job is due, but at most the poll interval."""
        async with self.db.sessionmaker() as session:
            due_in = await seconds_until_next(session)
        if due_in is None:
            return POLL_INTERVAL_SECONDS
        return min(max(due_in, MIN_SLEEP_SECONDS), POLL_INTERVAL_SECONDS)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    db = Database(settings, application_name="showcase-worker")
    http = httpx.AsyncClient(timeout=settings.webhook_timeout_seconds, follow_redirects=False)
    worker = Worker(settings, db, S3Storage(settings), Publisher(settings, db.sessionmaker, http))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # Wait for the database instead of crash-looping while it starts.
    delay = 1.0
    while not stop.is_set():
        try:
            await db.ping()
            break
        except Exception as exc:
            log.warning("worker_db_unavailable", error=repr(exc), retry_in=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    log.info(
        "worker_started",
        env=settings.app_env,
        webhook=bool(settings.revalidate_webhook_url),
        cdn_purge=settings.cdn_purge_provider,
    )
    try:
        while not stop.is_set():
            try:
                handled = await worker.run_once()
                sleep_for = 0.0 if handled else await worker.idle_seconds()
            except Exception:
                log.exception("worker_iteration_failed")
                sleep_for = POLL_INTERVAL_SECONDS
            if sleep_for:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=sleep_for)
    finally:
        await http.aclose()
        await db.dispose()
        log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
