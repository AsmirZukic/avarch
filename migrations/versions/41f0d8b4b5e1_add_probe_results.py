"""add probe results

Revision ID: 41f0d8b4b5e1
Revises: b3f6c2a51e8d
Create Date: 2026-06-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "41f0d8b4b5e1"
down_revision: str | None = "b3f6c2a51e8d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proberesult",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("media_file_id", sa.Integer(), nullable=False),
        sa.Column("ffprobe_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("normalized_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("probe_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["media_file_id"], ["mediafile.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_proberesult_media_file_id", "proberesult", ["media_file_id"])
    op.create_index("ix_proberesult_probe_hash", "proberesult", ["probe_hash"])


def downgrade() -> None:
    op.drop_index("ix_proberesult_probe_hash", table_name="proberesult")
    op.drop_index("ix_proberesult_media_file_id", table_name="proberesult")
    op.drop_table("proberesult")
