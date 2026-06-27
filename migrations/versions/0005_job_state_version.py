"""add job state version

Revision ID: 0005_job_state_version
Revises: 0004_promotion_target_lock
Create Date: 2026-06-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_job_state_version"
down_revision: str | None = "0004_promotion_target_lock"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("job")}
    if "state_version" not in columns:
        op.add_column(
            "job",
            sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("job")}
    if "state_version" in columns:
        op.drop_column("job", "state_version")
