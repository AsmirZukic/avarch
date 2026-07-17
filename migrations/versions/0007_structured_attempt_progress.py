"""add structured attempt progress metrics

Revision ID: 0007_structured_attempt_progress
Revises: 0006_job_attempt_progress
Create Date: 2026-07-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_structured_attempt_progress"
down_revision: str | None = "0006_job_attempt_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("job_attempt_progress")
    }
    for name in (
        "chunks_current",
        "chunks_total",
        "bitrate_kbps",
        "estimated_output_bytes",
        "written_output_bytes",
    ):
        if name not in existing:
            op.add_column("job_attempt_progress", sa.Column(name, sa.Integer(), nullable=True))


def downgrade() -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("job_attempt_progress")
    }
    for name in (
        "written_output_bytes",
        "estimated_output_bytes",
        "bitrate_kbps",
        "chunks_total",
        "chunks_current",
    ):
        if name in existing:
            op.drop_column("job_attempt_progress", name)
