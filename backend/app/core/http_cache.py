"""HTTP caching helpers: weak ETags, If-None-Match handling and Cache-Control policies.

Public read endpoints derive their ETag from `websites.content_version` (bumped on every
change affecting a site), so a conditional request is answered with a 304 after a single
indexed lookup, without running any content query.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import Request, Response
from pydantic import BaseModel

# Bump when the shape of public responses changes, so clients/CDNs drop cached bodies
# even though no site content changed.
PUBLIC_SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class CachePolicy:
    max_age: int
    s_maxage: int
    stale_while_revalidate: int

    @property
    def header(self) -> str:
        return (
            f"public, max-age={self.max_age}, s-maxage={self.s_maxage}, "
            f"stale-while-revalidate={self.stale_while_revalidate}"
        )


# Site content: CDN keeps it for 5 minutes, then serves stale while revalidating.
CONTENT_CACHE = CachePolicy(max_age=60, s_maxage=300, stale_while_revalidate=86_400)
# Host → site mapping: shorter, domain changes should propagate quickly.
ROUTING_CACHE = CachePolicy(max_age=30, s_maxage=60, stale_while_revalidate=600)


def _digest(*parts: str) -> str:
    return hashlib.blake2b("\x1f".join(parts).encode(), digest_size=10).hexdigest()


def version_etag(
    scope: str, version: int, endpoint: str, params: Mapping[str, object] | None = None
) -> str:
    """W/"<scope>:<version>:<endpoint>:<hash(params)>" — cheap, no body needed."""
    items = sorted((k, "" if v is None else str(v)) for k, v in (params or {}).items())
    params_hash = _digest(PUBLIC_SCHEMA_VERSION, *(f"{k}={v}" for k, v in items))
    return f'W/"{scope}:{version}:{endpoint}:{params_hash}"'


def body_etag(body: bytes) -> str:
    """ETag derived from the serialized body (for small cross-site responses)."""
    return f'W/"{_digest(PUBLIC_SCHEMA_VERSION, body.decode())}"'


def _opaque(tag: str) -> str:
    tag = tag.strip()
    return tag[2:] if tag.startswith("W/") else tag


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """Weak comparison (RFC 9110 §13.1.2) against an If-None-Match header value."""
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    wanted = _opaque(etag)
    return any(_opaque(candidate) == wanted for candidate in if_none_match.split(","))


def _headers(etag: str, policy: CachePolicy, *, vary: bool = False) -> dict[str, str]:
    # 200s get `Vary: Accept-Encoding` from GZipMiddleware when compressed; 304s bypass
    # it, so they carry the header themselves (RFC 9110 §15.4.5).
    headers = {"ETag": etag, "Cache-Control": policy.header}
    if vary:
        headers["Vary"] = "Accept-Encoding"
    return headers


def not_modified(request: Request, etag: str, policy: CachePolicy) -> Response | None:
    """A 304 response if the client already holds `etag`, else None."""
    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=_headers(etag, policy, vary=True))
    return None


def json_response(model: BaseModel, *, etag: str, policy: CachePolicy) -> Response:
    """Serialize with pydantic-core (fast, Rust) and attach cache headers."""
    return Response(
        content=model.model_dump_json(),
        media_type="application/json",
        headers=_headers(etag, policy),
    )


def body_cached_response(request: Request, model: BaseModel, policy: CachePolicy) -> Response:
    """For responses without a version counter: hash the body, 304 if unchanged."""
    body = model.model_dump_json().encode()
    etag = body_etag(body)
    return not_modified(request, etag, policy) or Response(
        content=body, media_type="application/json", headers=_headers(etag, policy)
    )
