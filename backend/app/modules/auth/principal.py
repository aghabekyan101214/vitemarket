"""The authenticated admin and what they may do.

Role matrix (PLAN.md §5):

| capability                        | admin | catalog_manager | site_editor        |
|-----------------------------------|-------|-----------------|--------------------|
| read websites / site data         | all   | all             | granted sites      |
| edit site content/SEO/theme/
  domains/assignments/publish       | all   | -               | granted sites      |
| create/enable/disable websites,
  verify domains                    | yes   | -               | -                  |
| products/translations (write)     | yes   | yes             | -                  |
| products (read)                   | yes   | yes             | yes (to assign)    |
| users, audit log                  | yes   | -               | -                  |
"""

import uuid
from dataclasses import dataclass

from app.core.errors import Forbidden
from app.modules.auth.models import AdminRole


@dataclass(frozen=True, slots=True)
class Principal:
    id: uuid.UUID
    email: str
    role: AdminRole
    website_ids: frozenset[uuid.UUID]  # grants (only meaningful for site_editor)

    @property
    def is_admin(self) -> bool:
        return self.role is AdminRole.ADMIN

    @property
    def readable_site_ids(self) -> frozenset[uuid.UUID] | None:
        """None = every site."""
        return self.website_ids if self.role is AdminRole.SITE_EDITOR else None

    def can_read_site(self, website_id: uuid.UUID) -> bool:
        return self.role is not AdminRole.SITE_EDITOR or website_id in self.website_ids

    def can_edit_site(self, website_id: uuid.UUID) -> bool:
        if self.role is AdminRole.ADMIN:
            return True
        return self.role is AdminRole.SITE_EDITOR and website_id in self.website_ids

    # --- guards (raise 403) -----------------------------------------------------------

    def require_admin(self) -> None:
        if not self.is_admin:
            raise Forbidden("Requires the admin role", code="role_required")

    def require_catalog_write(self) -> None:
        if self.role not in (AdminRole.ADMIN, AdminRole.CATALOG_MANAGER):
            raise Forbidden("Requires the admin or catalog_manager role", code="role_required")

    def require_site_read(self, website_id: uuid.UUID) -> None:
        if not self.can_read_site(website_id):
            raise Forbidden("No access to this website", code="site_access_denied")

    def require_site_edit(self, website_id: uuid.UUID) -> None:
        if not self.can_edit_site(website_id):
            raise Forbidden("Not allowed to edit this website", code="site_access_denied")
