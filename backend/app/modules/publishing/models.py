import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, TimestampMixin, pg_enum, uuid_pk


class JobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PublishJob(TimestampMixin, Base):
    """Outbox row: 'regenerate these tags/paths of this website'. Drained by the worker."""

    __tablename__ = "publish_jobs"
    __table_args__ = (
        # Debounce: at most one pending job per site; new changes merge into it.
        Index(
            "uq_publish_jobs_pending_per_site",
            "website_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_publish_jobs_due", "run_after", postgresql_where=text("status = 'pending'")),
        Index("ix_publish_jobs_website_created", "website_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    website_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("websites.id", ondelete="CASCADE"))
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"), server_default=JobStatus.PENDING.value
    )
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    paths: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    full_site: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    content_version: Mapped[int] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
