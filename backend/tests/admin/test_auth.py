import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import create_access_token
from app.models import AdminUser
from app.modules.auth.service import MAX_FAILED_LOGINS
from tests.admin.conftest import PASSWORD, Client, ok


async def _login(client: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    return await client.post("/auth/login", json={"email": email, "password": password})


async def test_login_and_me(as_role: Client, settings: Settings) -> None:
    anon = as_role("anon")
    body = await ok(await _login(anon, "  ADMIN@test.example ", PASSWORD))
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.jwt_access_ttl_minutes * 60
    me = await ok(
        await anon.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    )
    assert (me["email"], me["role"]) == ("admin@test.example", "admin")


async def test_site_editor_me_lists_grants(as_role: Client, site_ids: dict[str, uuid.UUID]) -> None:
    me = await ok(await as_role("site_editor").get("/auth/me"))
    assert me["website_ids"] == [str(site_ids["de"])]


async def test_login_errors_are_indistinguishable(as_role: Client) -> None:
    anon = as_role("anon")
    wrong = await _login(anon, "admin@test.example", "wrong-password!!")
    unknown = await _login(anon, "nobody@test.example", PASSWORD)
    malformed = await _login(anon, "not-an-email", PASSWORD)
    for r in (wrong, unknown, malformed):
        assert r.status_code == 401
        assert r.json()["code"] == "invalid_credentials"
        assert r.json()["detail"] == "Invalid email or password"


async def test_lockout(as_role: Client, db_session: AsyncSession) -> None:
    anon = as_role("anon")
    for _ in range(MAX_FAILED_LOGINS):
        assert (await _login(anon, "admin@test.example", "wrong-password!!")).status_code == 401
    # Locked: even the right password is refused.
    assert (await _login(anon, "admin@test.example", PASSWORD)).status_code == 401

    await db_session.execute(
        update(AdminUser)
        .where(AdminUser.email == "admin@test.example")
        .values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
    )
    await ok(await _login(anon, "admin@test.example", PASSWORD))


async def test_tokens(as_role: Client, settings: Settings, users: dict[str, uuid.UUID]) -> None:
    anon = as_role("anon")
    missing = await anon.get("/auth/me")
    assert (missing.status_code, missing.json()["code"]) == (401, "missing_token")
    assert missing.headers["www-authenticate"] == "Bearer"

    expired = create_access_token(
        users["admin"],
        secret=settings.jwt_secret.get_secret_value(),
        ttl_minutes=1,
        now=datetime.now(UTC) - timedelta(minutes=5),
    )
    forged = create_access_token(
        users["admin"], secret="another-secret-entirely-0123456789abcdef", ttl_minutes=5
    )
    for token in (expired.token, forged.token, "garbage"):
        r = await anon.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert (r.status_code, r.json()["code"]) == (401, "invalid_token")


async def test_deactivated_user_token_stops_working(
    as_role: Client, db_session: AsyncSession
) -> None:
    editor = as_role("site_editor")
    await ok(await editor.get("/auth/me"))
    await db_session.execute(
        update(AdminUser)
        .where(AdminUser.email == "site_editor@test.example")
        .values(is_active=False)
    )
    assert (await editor.get("/auth/me")).status_code == 401


async def test_admin_responses_are_not_cacheable(admin: httpx.AsyncClient) -> None:
    r = await admin.get("/websites")
    assert r.headers["cache-control"] == "no-store"
