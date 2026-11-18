"""Phase 7 hardening: body size limits, security headers, login rate limit, token revocation."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.ratelimit import RateLimiter
from app.core.security import create_access_token
from app.models import AdminUser
from tests.admin.conftest import PASSWORD, Client, ok


def test_rate_limiter_sliding_window() -> None:
    limiter = RateLimiter(2, 60)
    assert limiter.hit("a", now=0) is None
    assert limiter.hit("a", now=10) is None
    assert limiter.hit("a", now=20) == 40  # oldest hit (t=0) frees at t=60
    assert limiter.hit("b", now=20) is None  # independent key
    assert limiter.hit("a", now=60) is None  # window slid past t=0


def test_rate_limiter_bounded_keys() -> None:
    limiter = RateLimiter(1, 60, max_keys=4)
    for i in range(100):
        limiter.hit(f"ip{i}", now=float(i))
    assert len(limiter._hits) <= 4


async def test_security_headers(client: httpx.AsyncClient) -> None:
    r = await client.get("/health/live")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "strict-transport-security" not in r.headers  # only in production


async def test_hsts_in_production(settings: Settings) -> None:
    from app.main import create_app

    prod = create_app(settings.model_copy(update={"app_env": "production"}))
    transport = httpx.ASGITransport(app=prod)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/health/live")
    assert "max-age=" in r.headers["strict-transport-security"]


async def test_json_body_too_large(as_role: Client, settings: Settings) -> None:
    admin = as_role("admin")
    big = "x" * (settings.max_request_bytes + 1)
    r = await admin.post(
        "/websites",
        content=f'{{"key": "{big}"}}'.encode(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413
    assert r.json()["code"] == "payload_too_large"


async def test_streamed_body_too_large(as_role: Client, settings: Settings) -> None:
    """No Content-Length (chunked): bytes are counted as they arrive."""

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(settings.max_request_bytes // 65536 + 2):
            yield b" " * 65536

    r = await as_role("admin").post(
        "/websites", content=chunks(), headers={"content-type": "application/json"}
    )
    assert r.status_code == 413


async def test_login_rate_limit(app: FastAPI, as_role: Client, settings: Settings) -> None:
    anon = as_role("anon")
    headers = {"cf-connecting-ip": "203.0.113.7"}
    body = {"email": "admin@test.example", "password": "wrong-password!!"}
    for _ in range(settings.login_rate_limit_per_minute):
        r = await anon.post("/auth/login", json=body, headers=headers)
        assert r.status_code == 401
    r = await anon.post("/auth/login", json=body, headers=headers)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    # Another client IP is unaffected.
    r = await anon.post(
        "/auth/login",
        json={"email": "catalog_manager@test.example", "password": PASSWORD},
        headers={"cf-connecting-ip": "203.0.113.8"},
    )
    assert r.status_code == 200


async def test_password_change_revokes_existing_tokens(
    as_role: Client,
    db_session: AsyncSession,
    settings: Settings,
    users: dict[str, uuid.UUID],
) -> None:
    secret = settings.jwt_secret.get_secret_value()
    user_id = users["site_editor"]
    # A token issued a while ago (iat has 1-second resolution).
    old = create_access_token(
        user_id, secret=secret, ttl_minutes=60, now=datetime.now(UTC) - timedelta(minutes=5)
    )
    anon = as_role("anon")
    auth = {"Authorization": f"Bearer {old.token}"}
    await ok(await anon.get("/auth/me", headers=auth))

    await ok(
        await as_role("admin").patch(
            f"/users/{user_id}", json={"password": "a-brand-new-password-1"}
        )
    )
    user = await db_session.get(AdminUser, user_id, populate_existing=True)
    assert user is not None
    assert user.password_changed_at is not None

    r = await anon.get("/auth/me", headers=auth)
    assert r.status_code == 401

    fresh = await ok(
        await anon.post(
            "/auth/login",
            json={"email": "site_editor@test.example", "password": "a-brand-new-password-1"},
        )
    )
    await ok(
        await anon.get("/auth/me", headers={"Authorization": f"Bearer {fresh['access_token']}"})
    )
