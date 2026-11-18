"""deleted media

Revision ID: edabcf533dc9
Revises: 1d82138d5f23
Create Date: 2026-11-11 19:01:58.553816+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "edabcf533dc9"
down_revision: str | None = "1d82138d5f23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deleted_media",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deleted_media")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_deleted_media_storage_key")),
    )
    op.create_index("ix_deleted_media_purge_after", "deleted_media", ["purge_after"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_deleted_media_purge_after", table_name="deleted_media")
    op.drop_table("deleted_media")
