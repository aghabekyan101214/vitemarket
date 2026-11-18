"""admin password changed at

Revision ID: bae3421d73e3
Revises: edabcf533dc9
Create Date: 2026-11-11 19:20:49.731166+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bae3421d73e3"
down_revision: str | None = "edabcf533dc9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "admin_users", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("admin_users", "password_changed_at")
