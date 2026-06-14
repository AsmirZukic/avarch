"""add canonical probe ownership

Revision ID: 7d56d2f55f31
Revises: 41f0d8b4b5e1
Create Date: 2026-06-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d56d2f55f31"
down_revision: str | None = "41f0d8b4b5e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    media_columns = {column["name"] for column in inspector.get_columns("mediafile")}
    probe_columns = {column["name"] for column in inspector.get_columns("proberesult")}
    media_indexes = {index["name"] for index in inspector.get_indexes("mediafile")}
    media_foreign_keys = {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys("mediafile")
    }
    legacy_fingerprint_column = "content" + "_key"

    if "source_fs_fingerprint" not in probe_columns:
        with op.batch_alter_table("proberesult") as batch_op:
            batch_op.add_column(sa.Column("source_fs_fingerprint", sa.String(), nullable=True))

    if legacy_fingerprint_column in media_columns:
        if "ix_mediafile_" + legacy_fingerprint_column in media_indexes:
            op.drop_index("ix_mediafile_" + legacy_fingerprint_column, table_name="mediafile")
            media_indexes.remove("ix_mediafile_" + legacy_fingerprint_column)
        if bind.dialect.name == "sqlite":
            op.execute(
                f"ALTER TABLE mediafile RENAME COLUMN {legacy_fingerprint_column} "
                "TO fs_fingerprint"
            )
        else:
            with op.batch_alter_table("mediafile") as batch_op:
                batch_op.alter_column(
                    legacy_fingerprint_column,
                    new_column_name="fs_fingerprint",
                    existing_type=sa.String(),
                    existing_nullable=False,
                )
        media_columns.remove(legacy_fingerprint_column)
        media_columns.add("fs_fingerprint")

    if "ix_mediafile_fs_fingerprint" not in media_indexes:
        op.create_index("ix_mediafile_fs_fingerprint", "mediafile", ["fs_fingerprint"])

    with op.batch_alter_table("mediafile") as batch_op:
        if "latest_probe_id" not in media_columns:
            batch_op.add_column(sa.Column("latest_probe_id", sa.Integer(), nullable=True))
        if ("latest_probe_id",) not in media_foreign_keys:
            batch_op.create_foreign_key(
                "fk_mediafile_latest_probe_id_proberesult",
                "proberesult",
                ["latest_probe_id"],
                ["id"],
            )

    if "ix_mediafile_latest_probe_id" not in media_indexes:
        op.create_index("ix_mediafile_latest_probe_id", "mediafile", ["latest_probe_id"])


def downgrade() -> None:
    with op.batch_alter_table("mediafile") as batch_op:
        batch_op.drop_constraint("fk_mediafile_latest_probe_id_proberesult", type_="foreignkey")
        batch_op.drop_index("ix_mediafile_latest_probe_id")
        batch_op.drop_column("latest_probe_id")

    with op.batch_alter_table("proberesult") as batch_op:
        batch_op.drop_column("source_fs_fingerprint")
