"""Per-request mutation context shared by admin services.

Collects audit entries and affected websites while a service runs; `commit()` then
applies `mark_changed` (version bump + debounced publish job) and the audit rows in the
same transaction as the change itself.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.audit.models import AuditLog
from app.modules.auth.principal import Principal
from app.modules.publishing.impact import Change
from app.modules.publishing.service import mark_changed


@dataclass
class MutationContext:
    session: AsyncSession
    principal: Principal
    settings: Settings
    _changes: dict[uuid.UUID, Change] = field(default_factory=dict)

    def audit(
        self,
        action: str,
        entity: str,
        entity_id: object,
        *,
        website_id: uuid.UUID | None = None,
        diff: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            AuditLog(
                admin_user_id=self.principal.id,
                action=action,
                entity=entity,
                entity_id=str(entity_id),
                website_id=website_id,
                diff=jsonable_encoder(diff) if diff else None,
            )
        )

    def changed(self, website_id: uuid.UUID, change: Change) -> None:
        self._changes.setdefault(website_id, Change()).merge(change)

    async def commit(self) -> None:
        await self.session.flush()
        await mark_changed(
            self.session, self._changes, debounce_seconds=self.settings.publish_debounce_seconds
        )
        self._changes.clear()
        await self.session.commit()
