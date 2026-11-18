"""Admin edit → outbox → worker → webhook, end to end (worker run in-process)."""

import json
import uuid

import httpx
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models import Product, PublishJob
from app.modules.publishing.models import JobStatus
from app.modules.publishing.processor import Publisher
from tests.admin.conftest import ok


async def test_edit_is_published_and_failed_job_can_be_retried(
    admin: httpx.AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    site_ids: dict[str, uuid.UUID],
) -> None:
    deliveries: list[dict[str, object]] = []
    status = {"code": 500}

    def frontend(request: httpx.Request) -> httpx.Response:
        deliveries.append(json.loads(request.content))
        return httpx.Response(status["code"])

    publisher = Publisher(
        settings.model_copy(
            update={
                "revalidate_webhook_url": "https://frontend.test/revalidate",
                "revalidate_webhook_secret": SecretStr("s3cr3t"),
                "publish_max_attempts": 1,
            }
        ),
        async_sessionmaker(
            bind=db_session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(frontend)),
    )
    rug = await db_session.scalar(select(Product.id).where(Product.sku == "NH-RUG-JUTE"))
    await ok(await admin.patch(f"/products/{rug}", json={"attributes": {"pile": "flat"}}))
    await db_session.execute(update(PublishJob).values(run_after=PublishJob.created_at))
    await db_session.commit()

    assert await publisher.run_once() == 2  # the rug is on both sites
    assert {d["website_key"] for d in deliveries} == {"nordic-home-de", "nordic-home-fr"}
    jobs = await ok(await admin.get(f"/websites/{site_ids['fr']}/publish-jobs"))
    assert jobs[0]["status"] == "failed"  # max_attempts=1

    retried = await ok(await admin.post(f"/publish-jobs/{jobs[0]['id']}/retry"), 202)
    assert retried["status"] == "pending"
    assert (await admin.post(f"/publish-jobs/{retried['id']}/retry")).status_code == 409

    status["code"] = 200
    deliveries.clear()
    assert await publisher.run_once() == 1
    assert [d["website_key"] for d in deliveries] == ["nordic-home-fr"]
    site = await ok(await admin.get(f"/websites/{site_ids['fr']}"))
    assert not site["has_unpublished_changes"]
    assert (
        await db_session.scalar(
            select(PublishJob.status).where(PublishJob.id == uuid.UUID(retried["id"]))
        )
    ) is JobStatus.SUCCEEDED
