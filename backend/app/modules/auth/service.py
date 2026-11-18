import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, Unauthorized, ValidationFailed
from app.core.logging import get_logger
from app.core.security import (
    MIN_PASSWORD_LENGTH,
    burn_password_check,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.modules.auth.models import AdminRole, AdminUser, AdminUserWebsite
from app.modules.auth.principal import Principal
from app.modules.auth.repository import AdminUserRepository
from app.modules.websites.models import Website

if TYPE_CHECKING:
    from app.modules.context import MutationContext

log = get_logger(__name__)


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    local, sep, domain = email.partition("@")
    if not sep or not local or "." not in domain:
        raise ValidationFailed(f"Invalid email address: {email!r}")
    return email


async def create_admin_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    role: AdminRole,
    website_keys: Sequence[str] = (),
    reset_password_if_exists: bool = False,
) -> tuple[AdminUser, bool]:
    """Create an admin user (or reset an existing user's password when asked).

    Returns (user, created). Does not commit; the caller owns the transaction.
    """
    email = normalize_email(email)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationFailed(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if role is AdminRole.SITE_EDITOR and not website_keys:
        raise ValidationFailed("A site_editor must be granted at least one website")

    website_ids = []
    if website_keys:
        rows = await session.execute(
            select(Website.key, Website.id).where(Website.key.in_(website_keys))
        )
        found = {key: website_id for key, website_id in rows}
        missing = sorted(set(website_keys) - set(found))
        if missing:
            raise NotFound(f"Unknown website key(s): {', '.join(missing)}")
        website_ids = list(found.values())

    repo = AdminUserRepository(session)
    user = await repo.get_by_email(email)
    if user is not None:
        if not reset_password_if_exists:
            raise Conflict(f"Admin user {email} already exists", code="admin_exists")
        user.password_hash = hash_password(password)
        user.password_changed_at = datetime.now(UTC)  # revokes older tokens
        user.role = role
        user.is_active = True
        user.failed_logins = 0
        user.locked_until = None
        created = False
    else:
        user = await repo.add(
            AdminUser(email=email, password_hash=hash_password(password), role=role)
        )
        created = True

    await repo.set_website_grants(user.id, website_ids)
    return user, created


# --- login ------------------------------------------------------------------------------

MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
_INVALID = "Invalid email or password"


async def authenticate(session: AsyncSession, email: str, password: str) -> AdminUser:
    """Verify credentials; lock the account for 15 minutes after 5 failures in a row.

    Always answers with the same error (unknown user, wrong password, locked, inactive),
    so the endpoint can't be used to enumerate accounts. Commits the attempt bookkeeping.
    """
    try:
        email = normalize_email(email)
    except ValidationFailed:
        burn_password_check(password)
        raise Unauthorized(_INVALID, code="invalid_credentials") from None

    user = await session.scalar(select(AdminUser).where(AdminUser.email == email).with_for_update())
    now = datetime.now(UTC)
    if user is None or not user.is_active:
        burn_password_check(password)
        raise Unauthorized(_INVALID, code="invalid_credentials")
    if user.locked_until and user.locked_until > now:
        burn_password_check(password)
        log.warning("login_locked", user_id=str(user.id))
        raise Unauthorized(_INVALID, code="invalid_credentials")

    if not verify_password(user.password_hash, password):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.failed_logins = 0
            user.locked_until = now + LOCKOUT
            log.warning("login_lockout", user_id=str(user.id))
        await session.commit()
        raise Unauthorized(_INVALID, code="invalid_credentials")

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    await session.commit()
    return user


async def load_principal(
    session: AsyncSession, user_id: uuid.UUID, *, token_issued_at: datetime | None = None
) -> Principal | None:
    """Active user + website grants, or None when the token must not be accepted: user
    deleted or deactivated, or the token predates the last password change."""
    user = await session.get(AdminUser, user_id)
    if user is None or not user.is_active:
        return None
    changed = user.password_changed_at
    # JWT iat has 1 s resolution: compare at that resolution.
    if token_issued_at and changed and token_issued_at < changed.replace(microsecond=0):
        return None
    grants = await session.scalars(
        select(AdminUserWebsite.website_id).where(AdminUserWebsite.admin_user_id == user_id)
    )
    return Principal(id=user.id, email=user.email, role=user.role, website_ids=frozenset(grants))


# --- user management (admin only; callers check the role) -------------------------------


async def _existing_website_ids(
    session: AsyncSession, website_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    found = set(await session.scalars(select(Website.id).where(Website.id.in_(list(website_ids)))))
    missing = set(website_ids) - found
    if missing:
        raise NotFound(
            f"Unknown website id(s): {', '.join(sorted(map(str, missing)))}",
            code="website_not_found",
        )
    return sorted(found)


async def grant_websites(
    session: AsyncSession, user: AdminUser, website_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    if user.role is AdminRole.SITE_EDITOR and not website_ids:
        raise ValidationFailed("A site_editor must be granted at least one website")
    ids = await _existing_website_ids(session, website_ids)
    await AdminUserRepository(session).set_website_grants(user.id, ids)
    return ids


async def website_grants(
    session: AsyncSession, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    rows = await session.execute(
        select(AdminUserWebsite.admin_user_id, AdminUserWebsite.website_id)
        .where(AdminUserWebsite.admin_user_id.in_(list(user_ids)))
        .order_by(AdminUserWebsite.website_id)
    )
    grants: dict[uuid.UUID, list[uuid.UUID]] = {uid: [] for uid in user_ids}
    for uid, wid in rows:
        grants[uid].append(wid)
    return grants


async def register_user(
    ctx: "MutationContext",
    *,
    email: str,
    password: str,
    role: AdminRole,
    website_ids: Sequence[uuid.UUID],
) -> AdminUser:
    ctx.principal.require_admin()
    email = normalize_email(email)
    if role is AdminRole.SITE_EDITOR and not website_ids:
        raise ValidationFailed("A site_editor must be granted at least one website")
    if await AdminUserRepository(ctx.session).get_by_email(email) is not None:
        raise Conflict(f"Admin user {email} already exists", code="admin_exists")
    user = await AdminUserRepository(ctx.session).add(
        AdminUser(email=email, password_hash=hash_password(password), role=role)
    )
    granted = await grant_websites(ctx.session, user, website_ids)
    await ctx.session.refresh(user)
    ctx.audit(
        "user.create",
        "admin_user",
        user.id,
        diff={"email": [None, email], "role": [None, role.value], "websites": [None, granted]},
    )
    return user


async def update_user(
    ctx: "MutationContext",
    user: AdminUser,
    *,
    role: AdminRole | None,
    is_active: bool | None,
    password: str | None,
) -> AdminUser:
    ctx.principal.require_admin()
    if user.id == ctx.principal.id and (
        (role is not None and role is not AdminRole.ADMIN) or is_active is False
    ):
        raise Conflict("You cannot demote or deactivate yourself", code="self_lockout")
    changes: dict[str, Any] = {}
    if role is not None and role is not user.role:
        if role is AdminRole.SITE_EDITOR:
            grants = await website_grants(ctx.session, [user.id])
            if not grants[user.id]:
                raise ValidationFailed(
                    "Grant websites (PUT /users/{id}/websites) before making this user a "
                    "site_editor"
                )
        changes["role"] = [user.role.value, role.value]
        user.role = role
    if is_active is not None and is_active != user.is_active:
        changes["is_active"] = [user.is_active, is_active]
        user.is_active = is_active
    if password is not None:
        user.password_hash = hash_password(password)
        user.password_changed_at = datetime.now(UTC)  # revokes older tokens
        user.failed_logins = 0
        user.locked_until = None
        changes["password"] = ["***", "***"]
    await ctx.session.flush()
    await ctx.session.refresh(user)
    if changes:
        ctx.audit("user.update", "admin_user", user.id, diff=changes)
    return user


async def set_user_websites(
    ctx: "MutationContext", user: AdminUser, website_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    ctx.principal.require_admin()
    before = (await website_grants(ctx.session, [user.id]))[user.id]
    granted = await grant_websites(ctx.session, user, website_ids)
    if before != granted:
        ctx.audit("user.grants", "admin_user", user.id, diff={"websites": [before, granted]})
    return granted
