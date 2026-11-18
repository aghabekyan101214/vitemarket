import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import AdminUser, AdminUserWebsite


class AdminUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_email(self, email: str) -> AdminUser | None:
        result = await self.session.execute(select(AdminUser).where(AdminUser.email == email))
        return result.scalar_one_or_none()

    async def add(self, user: AdminUser) -> AdminUser:
        self.session.add(user)
        await self.session.flush()
        return user

    async def set_website_grants(
        self, user_id: uuid.UUID, website_ids: Sequence[uuid.UUID]
    ) -> None:
        existing = await self.session.execute(
            select(AdminUserWebsite).where(AdminUserWebsite.admin_user_id == user_id)
        )
        for grant in existing.scalars():
            await self.session.delete(grant)
        self.session.add_all(
            AdminUserWebsite(admin_user_id=user_id, website_id=wid) for wid in set(website_ids)
        )
        await self.session.flush()
