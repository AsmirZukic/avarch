"""add job attempt progress

Revision ID: 0006_job_attempt_progress
Revises: 0005_job_state_version
Create Date: 2026-07-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0006_job_attempt_progress"
down_revision: str | None = "0005_job_state_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "job_attempt_progress" in tables:
        return

    op.create_table(
        "job_attempt_progress",
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("current_value", sa.Float(), nullable=True),
        sa.Column("total_value", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("rate_per_second", sa.Float(), nullable=True),
        sa.Column("speed_ratio", sa.Float(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("message", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("phase_started_at", sa.DateTime(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("advanced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["jobattempt.id"]),
        sa.PrimaryKeyConstraint("attempt_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "job_attempt_progress" in tables:
        op.drop_table("job_attempt_progress")
