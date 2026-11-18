"""Worker publishing cycle against the real database (each test rolled back)."""

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models import PublishJob, Website
from app.modules.publishing.cdn import PurgeError, PurgeHost
from app.modules.publishing.impact import Change
from app.modules.publishing.models import JobStatus
from app.modules.publishing.processor import Publisher
from app.modules.publishing.service import mark_changed
from app.modules.publishing.webhook import verify
from app.seed import SeedSummary, seed

SECRET = "webhook-test-secret"


class FakePurger:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], list[str], bool]] = []
        self.fail = False

    async def purge(
        self, hosts: Sequence[PurgeHost], paths: Sequence[str], *, full_site: bool
    ) -> None:
        if self.fail:
            raise PurgeError("cloudflare down")
        self.calls.append(([h.hostname for h in hosts], list(paths), full_site))


class Frontend:
    """Records webhook deliveries; `status` controls the response."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, text="ok" if self.status == 200 else "boom")

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
async def seeded(db_session: AsyncSession) -> SeedSummary:
    summary = await seed(db_session)
    await db_session.commit()
    return summary


@pytest.fixture
def frontend() -> Frontend:
    return Frontend()


@pytest.fixture
def purger() -> FakePurger:
    return FakePurger()


def _publisher(
    db_session: AsyncSession, settings: Settings, frontend: Frontend, purger: FakePurger, **cfg: Any
) -> Publisher:
    cfg = {
        "revalidate_webhook_url": "https://frontend.test/api/revalidate",
        "revalidate_webhook_secret": SecretStr(SECRET),
        "publish_max_attempts": 3,
        **cfg,
    }
    sessionmaker = async_sessionmaker(
        bind=db_session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(frontend.handler))
    return Publisher(settings.model_copy(update=cfg), sessionmaker, http, purger)


@pytest.fixture
def publisher(
    db_session: AsyncSession, settings: Settings, frontend: Frontend, purger: FakePurger
) -> Publisher:
    return _publisher(db_session, settings, frontend, purger)


async def _enqueue(
    session: AsyncSession, website_id: uuid.UUID, *tags: str, due: bool = True
) -> None:
    await mark_changed(
        session,
        {website_id: Change(tags=set(tags), paths={"/products"})},
        debounce_seconds=0 if due else 3600,
    )
    await session.commit()


async def _jobs(session: AsyncSession, website_id: uuid.UUID) -> list[PublishJob]:
    rows = await session.scalars(
        select(PublishJob)
        .where(PublishJob.website_id == website_id)
        .order_by(PublishJob.id)  # uuidv7: creation order (now() is frozen in a tx)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def _website(session: AsyncSession, website_id: uuid.UUID) -> Website:
    site = await session.scalar(
        select(Website).where(Website.id == website_id).execution_options(populate_existing=True)
    )
    assert site is not None
    return site


async def test_successful_publish(
    db_session: AsyncSession,
    seeded: SeedSummary,
    publisher: Publisher,
    frontend: Frontend,
    purger: FakePurger,
) -> None:
    de = seeded.website_ids["nordic-home-de"]
    await _enqueue(db_session, de, "tag-a", "tag-b")
    version = (await _website(db_session, de)).content_version

    assert await publisher.run_once() == 1

    (request,) = frontend.requests
    assert verify(
        SECRET,
        request.headers["x-showcase-timestamp"],
        request.headers["x-showcase-signature"],
        request.content,
    )
    payload = frontend.payloads[0]
    assert payload["website_key"] == "nordic-home-de"
    assert payload["tags"] == ["tag-a", "tag-b"]
    assert payload["paths"] == ["/products"]
    assert payload["content_version"] == version
    assert payload["primary_host"] == "nordic-home-de.localhost"
    assert payload["hosts"] == ["nordic-home-de.localhost", "www.nordic-home-de.localhost"]
    assert request.headers["x-showcase-job-id"] == payload["job_id"]
    assert purger.calls == [(payload["hosts"], ["/products"], False)]

    (job,) = await _jobs(db_session, de)
    assert (job.status, job.attempts, job.finished_at is not None) == (JobStatus.SUCCEEDED, 1, True)
    site = await _website(db_session, de)
    assert site.published_version == version
    assert site.published_at is not None
    assert await publisher.run_once() == 0  # nothing left


async def test_jobs_wait_for_debounce(
    db_session: AsyncSession, seeded: SeedSummary, publisher: Publisher, frontend: Frontend
) -> None:
    await _enqueue(db_session, seeded.website_ids["nordic-home-de"], "t", due=False)
    assert await publisher.run_once() == 0
    assert frontend.requests == []


async def test_failure_backoff_then_failed(
    db_session: AsyncSession, seeded: SeedSummary, publisher: Publisher, frontend: Frontend
) -> None:
    de = seeded.website_ids["nordic-home-de"]
    await _enqueue(db_session, de, "t")
    frontend.status = 500

    before = datetime.now(UTC)
    assert await publisher.run_once() == 1
    (job,) = await _jobs(db_session, de)
    assert (job.status, job.attempts) == (JobStatus.PENDING, 1)
    assert "webhook returned 500" in (job.last_error or "")
    assert job.run_after >= before + timedelta(seconds=14)
    assert (await _website(db_session, de)).published_version == 0

    for attempt in (2, 3):  # publish_max_attempts = 3
        await db_session.execute(
            update(PublishJob).values(run_after=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db_session.commit()
        assert await publisher.run_once() == 1
        (job,) = await _jobs(db_session, de)
        assert job.attempts == attempt
    assert job.status is JobStatus.FAILED
    assert len(frontend.requests) == 3


async def test_failure_merges_into_newer_pending_job(
    db_session: AsyncSession,
    seeded: SeedSummary,
    publisher: Publisher,
    frontend: Frontend,
) -> None:
    de = seeded.website_ids["nordic-home-de"]
    await _enqueue(db_session, de, "old-tag")

    def fail_and_edit(request: httpx.Request) -> httpx.Response:
        frontend.requests.append(request)
        return httpx.Response(502)

    publisher.http = httpx.AsyncClient(transport=httpx.MockTransport(fail_and_edit))
    # Simulate an edit arriving while the job is running: claim, then enqueue, then fail.
    original_publish = publisher.publish

    async def publish_with_concurrent_edit(job: Any, target: Any) -> None:
        async with publisher.sessionmaker() as other:
            await _enqueue(other, de, "new-tag", due=False)
        await original_publish(job, target)

    publisher.publish = publish_with_concurrent_edit  # type: ignore[method-assign]
    assert await publisher.run_once() == 1

    failed, pending = await _jobs(db_session, de)
    assert failed.status is JobStatus.FAILED
    assert f"retrying as job {pending.id}" in (failed.last_error or "")
    assert pending.status is JobStatus.PENDING
    assert pending.tags == ["new-tag", "old-tag"]
    # The retry is scheduled by backoff (sooner than the newer job's 1 h debounce).
    assert pending.run_after < datetime.now(UTC) + timedelta(minutes=1)


async def test_cdn_failure_is_retried(
    db_session: AsyncSession, seeded: SeedSummary, publisher: Publisher, purger: FakePurger
) -> None:
    fr = seeded.website_ids["nordic-home-fr"]
    await _enqueue(db_session, fr, "t")
    purger.fail = True
    await publisher.run_once()
    (job,) = await _jobs(db_session, fr)
    assert job.status is JobStatus.PENDING
    assert "cloudflare down" in (job.last_error or "")


async def test_stuck_running_job_is_reset(
    db_session: AsyncSession, seeded: SeedSummary, publisher: Publisher, frontend: Frontend
) -> None:
    de = seeded.website_ids["nordic-home-de"]
    await _enqueue(db_session, de, "t")
    await db_session.execute(
        update(PublishJob).values(
            status=JobStatus.RUNNING,
            attempts=1,
            started_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await db_session.commit()

    assert await publisher.run_once() == 0  # reset, but not due yet (backoff)
    (job,) = await _jobs(db_session, de)
    assert job.status is JobStatus.PENDING
    assert "timed out" in (job.last_error or "")
    assert frontend.requests == []


async def test_without_webhook_config_only_marks_published(
    db_session: AsyncSession,
    seeded: SeedSummary,
    settings: Settings,
    frontend: Frontend,
    purger: FakePurger,
) -> None:
    publisher = _publisher(db_session, settings, frontend, purger, revalidate_webhook_url=None)
    de = seeded.website_ids["nordic-home-de"]
    await _enqueue(db_session, de, "t")
    assert await publisher.run_once() == 1
    assert frontend.requests == []
    (job,) = await _jobs(db_session, de)
    assert job.status is JobStatus.SUCCEEDED
