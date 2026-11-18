"""Drains the publish outbox: claim → webhook → CDN purge → mark published.

Safe with any number of worker processes: jobs are claimed with FOR UPDATE SKIP LOCKED,
and each job is committed separately. A failed job goes back to `pending` with
exponential backoff (or is merged into a pending job created meanwhile, since there can
be only one pending job per site), and becomes `failed` after `publish_max_attempts`.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.modules.publishing.cdn import CloudflarePurger, NoopPurger, PurgeHost, Purger
from app.modules.publishing.models import JobStatus, PublishJob
from app.modules.publishing.webhook import WebhookPayload, deliver
from app.modules.websites.models import DomainStatus, Website, WebsiteDomain

log = get_logger(__name__)

BACKOFF_BASE_SECONDS = 15
BACKOFF_MAX_SECONDS = 15 * 60
CLAIM_BATCH = 10


def backoff(attempts: int) -> timedelta:
    """15 s, 30 s, 1 min, 2 min, 4 min, 8 min, 15 min, 15 min, …"""
    return timedelta(
        seconds=min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS)
    )


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: uuid.UUID
    website_id: uuid.UUID
    tags: list[str]
    paths: list[str]
    full_site: bool
    content_version: int
    attempts: int


# --- queue operations -----------------------------------------------------------------


async def claim_due(session: AsyncSession, limit: int = CLAIM_BATCH) -> list[ClaimedJob]:
    due = (
        select(PublishJob.id)
        .where(PublishJob.status == JobStatus.PENDING, PublishJob.run_after <= func.now())
        .order_by(PublishJob.run_after)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    rows = await session.execute(
        update(PublishJob)
        .where(PublishJob.id.in_(due))
        .values(
            status=JobStatus.RUNNING,
            started_at=func.now(),
            attempts=PublishJob.attempts + 1,
            updated_at=func.now(),
        )
        .returning(
            PublishJob.id,
            PublishJob.website_id,
            PublishJob.tags,
            PublishJob.paths,
            PublishJob.full_site,
            PublishJob.content_version,
            PublishJob.attempts,
        )
        .execution_options(synchronize_session=False)
    )
    jobs = [ClaimedJob(*row) for row in rows]
    await session.commit()
    return jobs


async def mark_succeeded(session: AsyncSession, job: ClaimedJob) -> None:
    await session.execute(
        update(PublishJob)
        .where(PublishJob.id == job.id)
        .values(
            status=JobStatus.SUCCEEDED,
            finished_at=func.now(),
            last_error=None,
            updated_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(Website)
        .where(Website.id == job.website_id)
        .values(
            published_version=func.greatest(Website.published_version, job.content_version),
            published_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def requeue(
    session: AsyncSession, job: ClaimedJob, error: str, *, max_attempts: int
) -> JobStatus:
    """Back to pending with backoff, merged into a newer pending job if one exists, or
    `failed` once attempts are exhausted. Returns the job's resulting status."""
    error = error[:2000]
    if job.attempts >= max_attempts:
        await session.execute(
            update(PublishJob)
            .where(PublishJob.id == job.id)
            .values(
                status=JobStatus.FAILED,
                finished_at=func.now(),
                last_error=error,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return JobStatus.FAILED

    run_after = datetime.now(UTC) + backoff(job.attempts)
    try:
        async with session.begin_nested():
            await session.execute(
                update(PublishJob)
                .where(PublishJob.id == job.id)
                .values(
                    status=JobStatus.PENDING,
                    run_after=run_after,
                    last_error=error,
                    started_at=None,
                    updated_at=func.now(),
                )
                .execution_options(synchronize_session=False)
            )
        await session.commit()
        return JobStatus.PENDING
    except IntegrityError:
        pass  # a newer pending job exists for this site (one pending job per site)

    # Fold this job's work into the pending one (union of tags/paths, earliest retry).
    merged = await session.execute(
        text(
            """
            UPDATE publish_jobs p SET
              tags = ARRAY(SELECT DISTINCT v FROM unnest(p.tags || CAST(:tags AS text[])) v
                           ORDER BY v),
              paths = ARRAY(SELECT DISTINCT v FROM unnest(p.paths || CAST(:paths AS text[])) v
                            ORDER BY v),
              full_site = p.full_site OR :full_site,
              run_after = LEAST(p.run_after, :run_after),
              updated_at = now()
            WHERE p.website_id = :website_id AND p.status = 'pending'
            RETURNING p.id
            """
        ),
        {
            "tags": job.tags,
            "paths": job.paths,
            "full_site": job.full_site,
            "run_after": run_after,
            "website_id": job.website_id,
        },
    )
    into = merged.scalar_one_or_none()
    await session.execute(
        update(PublishJob)
        .where(PublishJob.id == job.id)
        .values(
            status=JobStatus.FAILED,
            finished_at=func.now(),
            last_error=f"{error} (retrying as job {into})",
            updated_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return JobStatus.FAILED


async def reset_stuck(session: AsyncSession, settings: Settings) -> int:
    """Requeue jobs left `running` by a crashed worker."""
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.publish_job_timeout_seconds)
    rows = await session.execute(
        select(
            PublishJob.id,
            PublishJob.website_id,
            PublishJob.tags,
            PublishJob.paths,
            PublishJob.full_site,
            PublishJob.content_version,
            PublishJob.attempts,
        )
        .where(PublishJob.status == JobStatus.RUNNING, PublishJob.started_at < cutoff)
        .with_for_update(skip_locked=True)
    )
    stuck = [ClaimedJob(*row) for row in rows]
    for job in stuck:
        log.warning("publish_job_stuck", job_id=str(job.id))
        await requeue(
            session,
            job,
            "timed out while running (worker crashed?)",
            max_attempts=settings.publish_max_attempts,
        )
    if not stuck:
        await session.rollback()
    return len(stuck)


async def seconds_until_next(session: AsyncSession) -> float | None:
    """Seconds until the earliest pending job is due (None = queue empty)."""
    value = await session.scalar(
        select(func.extract("epoch", func.min(PublishJob.run_after) - func.now())).where(
            PublishJob.status == JobStatus.PENDING
        )
    )
    await session.rollback()
    return None if value is None else max(float(value), 0.0)


# --- delivery -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SiteTarget:
    key: str
    locales: list[str]
    default_locale: str
    hosts: list[PurgeHost]
    primary_host: str | None


async def load_target(session: AsyncSession, website_id: uuid.UUID) -> SiteTarget | None:
    website = await session.get(Website, website_id)
    if website is None:
        return None
    domains = list(
        await session.scalars(
            select(WebsiteDomain)
            .where(
                WebsiteDomain.website_id == website_id,
                WebsiteDomain.status == DomainStatus.ACTIVE,
            )
            .order_by(WebsiteDomain.is_primary.desc(), WebsiteDomain.hostname)
        )
    )
    target = SiteTarget(
        key=website.key,
        locales=list(website.locales),
        default_locale=website.default_locale,
        hosts=[PurgeHost(d.hostname, d.cloudflare_zone_id) for d in domains],
        primary_host=next((d.hostname for d in domains if d.is_primary), None),
    )
    await session.rollback()  # read-only; don't hold a transaction during HTTP calls
    return target


class Publisher:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        http: httpx.AsyncClient,
        purger: Purger | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.http = http
        if purger is not None:
            self.purger: Purger = purger
        elif settings.cdn_purge_provider == "cloudflare" and settings.cloudflare_api_token:
            self.purger = CloudflarePurger(
                http,
                settings.cloudflare_api_token.get_secret_value(),
                settings.cloudflare_api_base_url,
            )
        else:
            self.purger = NoopPurger()

    async def publish(self, job: ClaimedJob, target: SiteTarget) -> None:
        url = self.settings.revalidate_webhook_url
        secret = self.settings.revalidate_webhook_secret
        if url and secret:
            payload = WebhookPayload(
                job_id=str(job.id),
                website_id=str(job.website_id),
                website_key=target.key,
                hosts=[h.hostname for h in target.hosts],
                primary_host=target.primary_host,
                locales=target.locales,
                default_locale=target.default_locale,
                tags=job.tags,
                paths=job.paths,
                full_site=job.full_site,
                content_version=job.content_version,
            )
            await deliver(self.http, url, secret.get_secret_value(), payload)
        else:
            log.warning("publish_webhook_not_configured", job_id=str(job.id))
        # Purge only after the frontend has regenerated, so the edge refetches fresh HTML.
        await self.purger.purge(target.hosts, job.paths, full_site=job.full_site)

    async def run_once(self) -> int:
        """Reset stuck jobs, then process one batch of due jobs. Returns jobs handled."""
        async with self.sessionmaker() as session:
            await reset_stuck(session, self.settings)
            jobs = await claim_due(session)
        for job in jobs:
            await self._process(job)
        return len(jobs)

    async def _process(self, job: ClaimedJob) -> None:
        log_ctx = {"job_id": str(job.id), "website_id": str(job.website_id)}
        async with self.sessionmaker() as session:
            target = await load_target(session, job.website_id)
            try:
                if target is None:
                    raise RuntimeError("website no longer exists")
                await self.publish(job, target)
            except Exception as exc:
                status = await requeue(
                    session, job, str(exc), max_attempts=self.settings.publish_max_attempts
                )
                log.warning(
                    "publish_job_failed",
                    **log_ctx,
                    attempts=job.attempts,
                    status=status.value,
                    error=str(exc),
                )
                return
            await mark_succeeded(session, job)
            log.info(
                "publish_job_succeeded",
                **log_ctx,
                tags=len(job.tags),
                paths=len(job.paths),
                full_site=job.full_site,
                content_version=job.content_version,
            )
