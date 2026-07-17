"""add scheduler runtime capacity fields

Revision ID: 0009_scheduler_runtime_capacity
Revises: 0008_scheduler_sessions_and_lifecycle_events
Create Date: 2026-07-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_scheduler_runtime_capacity"
down_revision: str | None = "0008_scheduler_sessions_and_lifecycle_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("capacity_cheap_workers", sa.Integer()),
    ("capacity_cheap_active", sa.Integer()),
    ("capacity_av1an_jobs", sa.Integer()),
    ("capacity_av1an_active", sa.Integer()),
    ("capacity_file_ops", sa.Integer()),
    ("capacity_file_ops_active", sa.Integer()),
    ("capacity_observed_at", sa.DateTime()),
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
