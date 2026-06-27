"""add job outcome reason

Revision ID: 0003_job_outcome_reason
Revises: 0002_media_plan
Create Date: 2026-06-24 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0003_job_outcome_reason"
down_revision: str | None = "0002_media_plan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("job")}
    if "outcome_reason" not in columns:
        op.add_column(
            "job",
            sa.Column("outcome_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("job")}
    if "outcome_reason" in columns:
        op.drop_column("job", "outcome_reason")