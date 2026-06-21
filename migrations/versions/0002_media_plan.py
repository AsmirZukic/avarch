"""add media plan table

Revision ID: 0002_media_plan
Revises: 0001_initial
Create Date: 2026-06-21 10:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0002_media_plan"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "mediaplan" in inspector.get_table_names():
        return

    op.create_table(
        "mediaplan",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("media_file_id", sa.Integer(), nullable=False),
        sa.Column("probe_result_id", sa.Integer(), nullable=False),
        sa.Column("profile_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("profile_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("probe_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_fs_fingerprint", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("execution_identity_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("plan_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("plan_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("output_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["media_file_id"], ["mediafile.id"]),
        sa.ForeignKeyConstraint(["probe_result_id"], ["proberesult.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mediaplan_execution_identity_hash"),
        "mediaplan",
        ["execution_identity_hash"],
        unique=False,
    )
    op.create_index(op.f("ix_mediaplan_is_current"), "mediaplan", ["is_current"], unique=False)
    op.create_index(op.f("ix_mediaplan_is_valid"), "mediaplan", ["is_valid"], unique=False)
    op.create_index(
        op.f("ix_mediaplan_media_file_id"),
        "mediaplan",
        ["media_file_id"],
        unique=False,
    )
    op.create_index(op.f("ix_mediaplan_plan_hash"), "mediaplan", ["plan_hash"], unique=True)
    op.create_index(op.f("ix_mediaplan_probe_hash"), "mediaplan", ["probe_hash"], unique=False)
    op.create_index(
        op.f("ix_mediaplan_probe_result_id"),
        "mediaplan",
        ["probe_result_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mediaplan_profile_hash"),
        "mediaplan",
        ["profile_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mediaplan_profile_name"),
        "mediaplan",
        ["profile_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mediaplan_source_fs_fingerprint"),
        "mediaplan",
        ["source_fs_fingerprint"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "mediaplan" not in inspector.get_table_names():
        return

    op.drop_index(op.f("ix_mediaplan_source_fs_fingerprint"), table_name="mediaplan")
    op.drop_index(op.f("ix_mediaplan_profile_name"), table_name="mediaplan")
    op.drop_index(op.f("ix_mediaplan_profile_hash"), table_name="mediaplan")
    op.drop_index(op.f("ix_mediaplan_probe_result_id"), table_name="mediaplan")
    op.drop_index(op.f("ix_mediaplan_probe_hash"), table_name="mediaplan")
    op.drop_index(op.f("ix_mediaplan_plan_hash"), table_name="mediaplan")
    op.drop_index(op.f("ix_mediaplan_media_file_id"), table_name="mediaplan")
    op.drop_index(op.f("ix_mediaplan_is_valid"), table_name="mediaplan")
    op.drop_index(op.f("ix_mediaplan_is_current"), table_name="mediaplan")
    op.drop_index(op.f("ix_mediaplan_execution_identity_hash"), table_name="mediaplan")
    op.drop_table("mediaplan")
