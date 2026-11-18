from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import publish_all
from app.models import PublishJob
from app.seed import seed


async def test_publish_all_queues_one_full_job_per_site(db_session: AsyncSession) -> None:
    summary = await seed(db_session)
    assert await publish_all(db_session) == summary.websites
    jobs = list(await db_session.scalars(select(PublishJob).where(PublishJob.status == "pending")))
    assert {j.website_id for j in jobs} == set(summary.website_ids.values())
    assert all(j.full_site for j in jobs)
    # Idempotent: merges into the pending jobs.
    await publish_all(db_session)
    assert len(list(await db_session.scalars(select(PublishJob)))) == len(jobs)
