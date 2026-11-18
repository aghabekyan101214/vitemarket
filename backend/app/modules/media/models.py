from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base


class DeletedMedia(Base):
    """Objects whose DB reference is gone, waiting for the delayed GC sweep.

    Deleting objects only after a grace period means statically generated pages that are
    still cached somewhere never point at a missing image.
    """

    __tablename__ = "deleted_media"
    __table_args__ = (Index("ix_deleted_media_purge_after", "purge_after"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    purge_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
