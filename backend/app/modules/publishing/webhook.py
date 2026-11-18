"""Signed revalidation webhook to the frontend (Next.js `revalidateTag`).

Signature scheme (verify on the receiving side, reject if |now - timestamp| > 5 min):

    X-Showcase-Timestamp: <unix seconds>
    X-Showcase-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>

`job_id` in the body is the idempotency key: a retried delivery carries the same id.
"""

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import asdict, dataclass

import httpx

SIGNATURE_VERSION = "v1"
TOLERANCE_SECONDS = 300
USER_AGENT = "showcase-publisher/1"


@dataclass(frozen=True, slots=True)
class WebhookPayload:
    job_id: str
    website_id: str
    website_key: str
    hosts: list[str]
    primary_host: str | None
    locales: list[str]
    default_locale: str
    tags: list[str]
    paths: list[str]
    full_site: bool
    content_version: int

    def body(self) -> bytes:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True).encode()


class WebhookError(Exception):
    pass


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"{SIGNATURE_VERSION}={mac.hexdigest()}"


def verify(
    secret: str,
    timestamp: str,
    signature: str,
    body: bytes,
    *,
    now: float | None = None,
    tolerance: int = TOLERANCE_SECONDS,
) -> bool:
    """Reference implementation of the receiver's check (port this to the frontend)."""
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, ts, body), signature)


async def deliver(
    client: httpx.AsyncClient, url: str, secret: str, payload: WebhookPayload
) -> None:
    body = payload.body()
    timestamp = int(time.time())
    try:
        response = await client.post(
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "X-Showcase-Timestamp": str(timestamp),
                "X-Showcase-Signature": sign(secret, timestamp, body),
                "X-Showcase-Job-Id": payload.job_id,
                "X-Request-ID": uuid.uuid4().hex,
            },
        )
    except httpx.HTTPError as exc:
        raise WebhookError(f"webhook request failed: {exc!r}") from exc
    if not response.is_success:
        raise WebhookError(f"webhook returned {response.status_code}: {response.text[:300]}")
