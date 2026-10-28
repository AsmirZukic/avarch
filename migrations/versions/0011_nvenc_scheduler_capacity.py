"""add NVENC scheduler capacity fields

Revision ID: 0011_nvenc_scheduler_capacity
Revises: 0010_promotion_safety
Create Date: 2026-10-28 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_nvenc_scheduler_capacity"
down_revision: str | None = "0010_promotion_safety"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("capacity_nvenc_jobs", sa.Integer()),
    ("capacity_nvenc_active", sa.Integer()),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("schedulerstate")}
    missing = [(name, column_type) for name, column_type in _COLUMNS if name not in existing]
    if not missing:
        return
    with op.batch_alter_table("schedulerstate") as batch:
        for name, column_type in missing:
            batch.add_column(sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("schedulerstate")}
    present = [name for name, _column_type in _COLUMNS if name in existing]
    if not present:
        return
    with op.batch_alter_table("schedulerstate") as batch:
        for name in reversed(present):
            batch.drop_column(name)
