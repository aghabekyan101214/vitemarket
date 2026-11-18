"""Transactional outbox: record that websites changed, debounced into one pending job.

`mark_changed` runs inside the admin's transaction. It bumps `content_version` (new public
ETags immediately) and, for auto-publishing sites, upserts the site's single pending job,
so a burst of edits yields one regeneration. A rolled-back transaction leaves no job.
"""

import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.publishing.impact import Change, site_tag
from app.modules.publishing.models import JobStatus, PublishJob
from app.modules.websites.models import Website

_PENDING: ColumnElement[Any] = literal_column("'pending'::job_status")


# Union of the pending job's array and the incoming one, deduplicated and sorted.
_MERGED_TAGS: ColumnElement[Any] = literal_column(
    "ARRAY(SELECT DISTINCT v FROM unnest(publish_jobs.tags || excluded.tags) AS v ORDER BY v)"
)
_MERGED_PATHS: ColumnElement[Any] = literal_column(
    "ARRAY(SELECT DISTINCT v FROM unnest(publish_jobs.paths || excluded.paths) AS v ORDER BY v)"
)


async def _upsert_jobs(
    session: AsyncSession, rows: list[dict[str, object]], *, delay: timedelta
) -> None:
    if not rows:
        return
    stmt = pg_insert(PublishJob).values(
        [{**r, "status": JobStatus.PENDING, "run_after": func.now() + delay} for r in rows]
    )
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[PublishJob.website_id],
        index_where=PublishJob.status == _PENDING,
        set_={
            "tags": _MERGED_TAGS,
            "paths": _MERGED_PATHS,
            "full_site": PublishJob.full_site | excluded.full_site,
            "content_version": excluded.content_version,
            "run_after": excluded.run_after,  # trailing debounce
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


async def mark_changed(
    session: AsyncSession, changes: Mapping[uuid.UUID, Change], *, debounce_seconds: int
) -> None:
    if not changes:
        return
    bumped = await session.execute(
        update(Website)
        .where(Website.id.in_(list(changes)))
        .values(content_version=Website.content_version + 1)
        .returning(Website.id, Website.content_version, Website.auto_publish)
    )
    rows = []
    for website_id, version, auto_publish in bumped:
        if not auto_publish:
            continue  # accumulates until someone presses Publish
        change = changes[website_id]
        rows.append(
            {
                "website_id": website_id,
                "tags": sorted(change.tags),
                "paths": sorted(change.paths),
                "full_site": change.full_site,
                "content_version": version,
            }
        )
    await _upsert_jobs(session, rows, delay=timedelta(seconds=debounce_seconds))


async def request_publish(session: AsyncSession, website_id: uuid.UUID) -> PublishJob:
    """Manual publish: regenerate the whole site now (merges into a pending job)."""
    version = await session.scalar(select(Website.content_version).where(Website.id == website_id))
    await _upsert_jobs(
        session,
        [
            {
                "website_id": website_id,
                "tags": [site_tag(website_id)],
                "paths": [],
                "full_site": True,
                "content_version": version,
            }
        ],
        delay=timedelta(0),
    )
    job = await session.scalar(
        select(PublishJob)
        .where(PublishJob.website_id == website_id, PublishJob.status == JobStatus.PENDING)
        .execution_options(populate_existing=True)
    )
    assert job is not None
    return job


async def retry_failed(session: AsyncSession, job: PublishJob) -> PublishJob:
    """Requeue a failed job's work now (merged into the site's pending job, if any)."""
    version = await session.scalar(
        select(Website.content_version).where(Website.id == job.website_id)
    )
    await _upsert_jobs(
        session,
        [
            {
                "website_id": job.website_id,
                "tags": list(job.tags),
                "paths": list(job.paths),
                "full_site": job.full_site,
                "content_version": version,
            }
        ],
        delay=timedelta(0),
    )
    pending = await session.scalar(
        select(PublishJob)
        .where(PublishJob.website_id == job.website_id, PublishJob.status == JobStatus.PENDING)
        .execution_options(populate_existing=True)
    )
    assert pending is not None
    return pending
