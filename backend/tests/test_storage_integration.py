"""Round trip against the real S3 endpoint (SeaweedFS locally, R2 in CI if configured)."""

import uuid

import httpx
import pytest

from app.core.config import Settings
from app.core.storage import IMMUTABLE_CACHE_CONTROL, S3Storage

pytestmark = pytest.mark.integration


@pytest.fixture
def s3(settings: Settings) -> S3Storage:
    try:
        httpx.get(settings.storage_endpoint_url, timeout=2)
    except httpx.HTTPError:
        pytest.skip("object storage is not reachable")
    return S3Storage(settings)


async def test_put_public_get_delete(s3: S3Storage, settings: Settings) -> None:
    key = f"tests/{uuid.uuid4().hex}.txt"
    await s3.put(key, b"hello", content_type="text/plain", cache_control=IMMUTABLE_CACHE_CONTROL)
    assert await s3.exists(key)

    # Anonymous read through the public endpoint (bucket is public-read, like R2 + domain).
    public = settings.storage_public_base_url.replace("localhost", "storage")
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{public}/{key}")
    assert r.status_code == 200
    assert r.content == b"hello"
    assert r.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL

    assert await s3.delete([key, f"tests/{uuid.uuid4().hex}-missing"]) == []
    assert not await s3.exists(key)
