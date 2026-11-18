"""Object storage: Cloudflare R2 in production, SeaweedFS locally — same S3 code path.

R2 rules honoured here (PLAN.md §0): region "auto", no ACLs (public access is per bucket
via its custom domain), and checksums only "when_required" so one client config works on
both R2 and SeaweedFS. boto3 is synchronous, so calls run in a worker thread.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.core.errors import ServiceUnavailable
from app.core.logging import get_logger

if TYPE_CHECKING:
    from types_boto3_s3 import S3Client

log = get_logger(__name__)

IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
_DELETE_BATCH = 1000  # S3 DeleteObjects limit


class StorageError(Exception):
    pass


class Storage(Protocol):
    async def put(
        self, key: str, data: bytes, *, content_type: str, cache_control: str
    ) -> None: ...

    async def delete(self, keys: Sequence[str]) -> list[str]:
        """Delete objects; returns the keys that could NOT be deleted."""
        ...

    async def exists(self, key: str) -> bool: ...


class S3Storage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.storage_bucket
        self._client: S3Client = boto3.client(
            "s3",
            endpoint_url=settings.storage_endpoint_url,
            region_name=settings.storage_region,
            aws_access_key_id=settings.storage_access_key_id,
            aws_secret_access_key=settings.storage_secret_access_key.get_secret_value(),
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )

    async def put(self, key: str, data: bytes, *, content_type: str, cache_control: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                CacheControl=cache_control,
            )
        except (BotoCoreError, ClientError) as exc:
            log.error("storage_put_failed", key=key, error=repr(exc))
            raise ServiceUnavailable("Object storage is unavailable") from exc

    async def delete(self, keys: Sequence[str]) -> list[str]:
        failed: list[str] = []
        for start in range(0, len(keys), _DELETE_BATCH):
            batch = list(keys[start : start + _DELETE_BATCH])
            try:
                response = await asyncio.to_thread(
                    self._client.delete_objects,
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
                )
            except (BotoCoreError, ClientError) as exc:
                log.error("storage_delete_failed", count=len(batch), error=repr(exc))
                failed.extend(batch)
                continue
            failed.extend(err["Key"] for err in response.get("Errors", []) if "Key" in err)
        return failed

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise StorageError(str(exc)) from exc
        return True


@dataclass
class StoredObject:
    data: bytes
    content_type: str
    cache_control: str


class MemoryStorage:
    """In-process storage for tests (and for running without S3)."""

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.fail_puts = False

    async def put(self, key: str, data: bytes, *, content_type: str, cache_control: str) -> None:
        if self.fail_puts:
            raise ServiceUnavailable("Object storage is unavailable")
        self.objects[key] = StoredObject(data, content_type, cache_control)

    async def delete(self, keys: Sequence[str]) -> list[str]:
        for key in keys:
            self.objects.pop(key, None)
        return []

    async def exists(self, key: str) -> bool:
        return key in self.objects
