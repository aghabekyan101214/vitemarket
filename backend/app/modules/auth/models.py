import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import Base, TimestampMixin, pg_enum, uuid_pk


class AdminRole(enum.StrEnum):
    ADMIN = "admin"  # everything
    CATALOG_MANAGER = "catalog_manager"  # products/translations/images, read all sites
    SITE_EDITOR = "site_editor"  # content/SEO/theme/assignments of assigned sites


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("position('@' in email) > 1", name="email_format"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[AdminRole] = mapped_column(pg_enum(AdminRole, "admin_role"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    failed_logins: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Tokens issued before this instant are rejected (password reset ⇒ sessions revoked).
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    website_grants: Mapped[list["AdminUserWebsite"]] = relationship(
        lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )


class AdminUserWebsite(Base):
    """Websites a site_editor may manage."""

    __tablename__ = "admin_user_websites"

    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True
    )
    website_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), primary_key=True, index=True
    )
