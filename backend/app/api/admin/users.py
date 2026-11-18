"""Admin users and the audit log (admin role only)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import select

from app.api.admin.deps import Ctx
from app.core.errors import NotFound
from app.modules.audit.models import AuditLog
from app.modules.auth import service
from app.modules.auth.models import AdminRole, AdminUser
from app.modules.auth.schemas import AuditEntryOut, GrantsIn, UserCreate, UserOut, UserUpdate

router = APIRouter(tags=["admin: users & audit"])


def _out(user: AdminUser, website_ids: list[uuid.UUID]) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        locked_until=user.locked_until,
        website_ids=website_ids,
        created_at=user.created_at,
    )


async def _get_user(ctx: Ctx, user_id: uuid.UUID) -> AdminUser:
    user = await ctx.session.get(AdminUser, user_id)
    if user is None:
        raise NotFound("User not found", code="user_not_found")
    return user


@router.get("/users", response_model=list[UserOut])
async def list_users(ctx: Ctx) -> list[UserOut]:
    ctx.principal.require_admin()
    users = list(await ctx.session.scalars(select(AdminUser).order_by(AdminUser.email)))
    grants = await service.website_grants(ctx.session, [u.id for u in users])
    return [_out(u, grants[u.id]) for u in users]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(ctx: Ctx, body: UserCreate) -> UserOut:
    user = await service.register_user(
        ctx,
        email=body.email,
        password=body.password,
        role=AdminRole(body.role),
        website_ids=body.website_ids,
    )
    await ctx.commit()
    grants = await service.website_grants(ctx.session, [user.id])
    return _out(user, grants[user.id])


@router.patch("/users/{user_id}", response_model=UserOut, summary="Role, activation, password")
async def update_user(ctx: Ctx, user_id: uuid.UUID, body: UserUpdate) -> UserOut:
    ctx.principal.require_admin()
    user = await _get_user(ctx, user_id)
    await service.update_user(
        ctx,
        user,
        role=AdminRole(body.role) if body.role else None,
        is_active=body.is_active,
        password=body.password,
    )
    await ctx.commit()
    grants = await service.website_grants(ctx.session, [user.id])
    return _out(user, grants[user.id])


@router.put("/users/{user_id}/websites", response_model=UserOut, summary="Website grants")
async def set_user_websites(ctx: Ctx, user_id: uuid.UUID, body: GrantsIn) -> UserOut:
    ctx.principal.require_admin()
    user = await _get_user(ctx, user_id)
    granted = await service.set_user_websites(ctx, user, body.website_ids)
    await ctx.commit()
    return _out(user, granted)


@router.get("/audit-log", response_model=list[AuditEntryOut], summary="Newest first")
async def audit_log(
    ctx: Ctx,
    website_id: uuid.UUID | None = None,
    entity: Annotated[str | None, Query(max_length=50)] = None,
    entity_id: Annotated[str | None, Query(max_length=100)] = None,
    admin_user_id: uuid.UUID | None = None,
    before_id: Annotated[
        int | None, Query(description="Cursor: entries older than this id")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AuditLog]:
    ctx.principal.require_admin()
    stmt = select(AuditLog)
    for column, value in (
        (AuditLog.website_id, website_id),
        (AuditLog.entity, entity),
        (AuditLog.entity_id, entity_id),
        (AuditLog.admin_user_id, admin_user_id),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
    if before_id is not None:
        stmt = stmt.where(AuditLog.id < before_id)
    return list(await ctx.session.scalars(stmt.order_by(AuditLog.id.desc()).limit(limit)))
