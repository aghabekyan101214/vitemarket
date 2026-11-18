import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base


class AuditLog(Base):
    """Append-only record of admin mutations."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_entity", "entity", "entity_id"),
        Index("ix_audit_log_website_created", "website_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(50))  # e.g. "product.update"
    entity: Mapped[str] = mapped_column(String(50))  # e.g. "product"
    entity_id: Mapped[str] = mapped_column(String(100))
    # Deliberately no FK: the log must survive deletion of the website.
    website_id: Mapped[uuid.UUID | None] = mapped_column()
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
