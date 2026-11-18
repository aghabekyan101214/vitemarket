import json
import time

import httpx
import pytest

from app.modules.publishing.cdn import CloudflarePurger, PurgeError, PurgeHost
from app.modules.publishing.processor import backoff
from app.modules.publishing.webhook import sign, verify

# --- signatures -----------------------------------------------------------------------


def test_signature_roundtrip() -> None:
    body = b'{"job_id":"1"}'
    now = int(time.time())
    signature = sign("secret", now, body)
    assert signature.startswith("v1=")
    assert verify("secret", str(now), signature, body)


@pytest.mark.parametrize(
    ("secret", "timestamp_offset", "body"),
    [
        ("other-secret", 0, b'{"job_id":"1"}'),  # wrong key
        ("secret", 0, b'{"job_id":"2"}'),  # tampered body
        ("secret", -301, b'{"job_id":"1"}'),  # replayed outside the 5 min window
    ],
)
def test_signature_rejections(secret: str, timestamp_offset: int, body: bytes) -> None:
    now = int(time.time())
    signature = sign("secret", now, b'{"job_id":"1"}')
    ts = now + timestamp_offset
    if timestamp_offset:
        signature = sign("secret", ts, body)
    assert not verify(secret, str(ts), signature, body)


def test_signature_rejects_garbage_timestamp() -> None:
    assert not verify("secret", "yesterday", "v1=00", b"")


def test_backoff() -> None:
    seconds = [backoff(n).total_seconds() for n in range(1, 10)]
    assert seconds == [15, 30, 60, 120, 240, 480, 900, 900, 900]


# --- Cloudflare purge -----------------------------------------------------------------


def _purger(calls: list[tuple[str, dict[str, list[str]]]], status: int = 200) -> CloudflarePurger:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer cf-token"
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(status, json={"success": status == 200, "errors": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CloudflarePurger(client, "cf-token", "https://cf.test/client/v4")


HOSTS = [PurgeHost("shop.example", "zone-1"), PurgeHost("www.shop.example", "zone-1")]


async def test_purge_by_url_in_batches() -> None:
    calls: list[tuple[str, dict[str, list[str]]]] = []
    paths = [f"/p{i}" for i in range(20)]  # 2 hosts x 20 = 40 URLs → 2 requests of ≤30
    await _purger(calls).purge(HOSTS, paths, full_site=False)
    assert [c[0] for c in calls] == ["/client/v4/zones/zone-1/purge_cache"] * 2
    urls = [u for _, body in calls for u in body["files"]]
    assert len(urls) == 40
    assert "https://www.shop.example/p3" in urls


@pytest.mark.parametrize(
    ("paths", "full_site"), [(["/"], True), ([f"/p{i}" for i in range(60)], False), ([], False)]
)
async def test_purge_hostnames(paths: list[str], full_site: bool) -> None:
    calls: list[tuple[str, dict[str, list[str]]]] = []
    await _purger(calls).purge(HOSTS, paths, full_site=full_site)
    assert calls == [
        ("/client/v4/zones/zone-1/purge_cache", {"hosts": ["shop.example", "www.shop.example"]})
    ]


async def test_hosts_without_zone_are_skipped() -> None:
    calls: list[tuple[str, dict[str, list[str]]]] = []
    await _purger(calls).purge([PurgeHost("x.example", None)], ["/"], full_site=True)
    assert calls == []


async def test_purge_failure_raises() -> None:
    with pytest.raises(PurgeError):
        await _purger([], status=403).purge(HOSTS, ["/"], full_site=False)
