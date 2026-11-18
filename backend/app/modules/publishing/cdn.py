"""CDN purge after revalidation (Cloudflare), for HTML cached at the edge.

Per host: purge by URL list, or the whole hostname when the change is site-wide or touches
many pages. Hosts without a `cloudflare_zone_id` are skipped.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.logging import get_logger

log = get_logger(__name__)

MAX_URLS_PER_JOB = 100  # beyond this, purging the hostname is cheaper and safer
URLS_PER_REQUEST = 30  # Cloudflare's per-request limit for URL purges on most plans


@dataclass(frozen=True, slots=True)
class PurgeHost:
    hostname: str
    zone_id: str | None


class PurgeError(Exception):
    pass


class Purger(Protocol):
    async def purge(
        self, hosts: Sequence[PurgeHost], paths: Sequence[str], *, full_site: bool
    ) -> None: ...


class NoopPurger:
    async def purge(
        self, hosts: Sequence[PurgeHost], paths: Sequence[str], *, full_site: bool
    ) -> None:
        return None


class CloudflarePurger:
    def __init__(self, client: httpx.AsyncClient, api_token: str, base_url: str) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {api_token}"}
        self.base_url = base_url.rstrip("/")

    async def _call(self, zone_id: str, body: dict[str, list[str]]) -> None:
        try:
            response = await self.client.post(
                f"{self.base_url}/zones/{zone_id}/purge_cache", json=body, headers=self.headers
            )
        except httpx.HTTPError as exc:
            raise PurgeError(f"Cloudflare purge failed: {exc!r}") from exc
        data = response.json() if response.content else {}
        if not response.is_success or not data.get("success", False):
            raise PurgeError(
                f"Cloudflare purge failed ({response.status_code}): {data.get('errors')}"
            )

    async def purge(
        self, hosts: Sequence[PurgeHost], paths: Sequence[str], *, full_site: bool
    ) -> None:
        by_zone: dict[str, list[str]] = defaultdict(list)
        for host in hosts:
            if host.zone_id:
                by_zone[host.zone_id].append(host.hostname)
            else:
                log.warning("cdn_purge_skipped_no_zone", host=host.hostname)
        for zone_id, hostnames in by_zone.items():
            urls = [f"https://{h}{p}" for h in hostnames for p in paths]
            if full_site or not paths or len(urls) > MAX_URLS_PER_JOB:
                await self._call(zone_id, {"hosts": hostnames})
                continue
            for start in range(0, len(urls), URLS_PER_REQUEST):
                await self._call(zone_id, {"files": urls[start : start + URLS_PER_REQUEST]})
