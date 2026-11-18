"""Small in-process sliding-window rate limiter (per key).

Limits are per API process; with N uvicorn workers the effective limit is up to N times
higher. That is acceptable as a second line of defence behind the Cloudflare rate-limit
rule on /admin/v1/auth/login (and the per-account lockout in the auth service).
"""

import time
from collections import deque


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float, *, max_keys: int = 10_000) -> None:
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str, now: float | None = None) -> float | None:
        """Record a hit. Returns None if allowed, else seconds until the next slot frees."""
        now = time.monotonic() if now is None else now
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self.max_keys:
                self._evict(now)
            hits = self._hits[key] = deque()
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return max(hits[0] + self.window - now, 0.0)
        hits.append(now)
        return None

    def _evict(self, now: float) -> None:
        stale = [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window]
        for key in stale:
            del self._hits[key]
        if len(self._hits) >= self.max_keys:  # still full: drop the oldest half
            for key in list(self._hits)[: self.max_keys // 2]:
                del self._hits[key]
