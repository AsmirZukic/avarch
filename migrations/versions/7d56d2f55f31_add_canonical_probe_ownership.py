"""add canonical probe ownership

Revision ID: 7d56d2f55f31
Revises: 41f0d8b4b5e1
Create Date: 2026-06-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "7d56d2f55f31"
down_revision: str | None = "41f0d8b4b5e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    media_columns = {column["name"] for column in inspector.get_columns("mediafile")}
    legacy_fingerprint_column = "content" + "_key"

    with op.batch_alter_table("proberesult") as batch_op:
        batch_op.add_column(
            sa.Column("source_fs_fingerprint", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )

    with op.batch_alter_table("mediafile") as batch_op:
        if legacy_fingerprint_column in media_columns:
            batch_op.drop_index("ix_mediafile_" + legacy_fingerprint_column)
            batch_op.alter_column(
                legacy_fingerprint_column,
                new_column_name="fs_fingerprint",
                existing_type=sqlmodel.sql.sqltypes.AutoString(),
                existing_nullable=False,
            )
            batch_op.create_index("ix_mediafile_fs_fingerprint", ["fs_fingerprint"], unique=False)

        batch_op.add_column(sa.Column("latest_probe_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_mediafile_latest_probe_id", ["latest_probe_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_mediafile_latest_probe_id_proberesult",
            "proberesult",
            ["latest_probe_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("mediafile") as batch_op:
        batch_op.drop_constraint("fk_mediafile_latest_probe_id_proberesult", type_="foreignkey")
        batch_op.drop_index("ix_mediafile_latest_probe_id")
        batch_op.drop_column("latest_probe_id")

    with op.batch_alter_table("proberesult") as batch_op:
        batch_op.drop_column("source_fs_fingerprint")
