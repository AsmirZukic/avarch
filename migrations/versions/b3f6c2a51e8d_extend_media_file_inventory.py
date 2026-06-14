"""extend media file inventory

Revision ID: b3f6c2a51e8d
Revises: 9aaf75d07ce6
Create Date: 2026-06-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "b3f6c2a51e8d"
down_revision: str | None = "9aaf75d07ce6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("mediafile") as batch_op:
        batch_op.add_column(sa.Column("device_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("inode", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("content_key", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
        batch_op.add_column(sa.Column("last_seen_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=True))

    connection = op.get_bind()
    mediafile = sa.table(
        "mediafile",
        sa.column("id", sa.Integer()),
        sa.column("path", sa.String()),
        sa.column("device_id", sa.Integer()),
        sa.column("inode", sa.Integer()),
        sa.column("content_key", sa.String()),
        sa.column("discovered_at", sa.DateTime()),
        sa.column("last_seen_at", sa.DateTime()),
        sa.column("status", sa.String()),
    )
    rows = connection.execute(sa.select(mediafile.c.id, mediafile.c.path)).mappings()
    for row in rows:
        connection.execute(
            mediafile.update()
            .where(mediafile.c.id == row["id"])
            .values(
                device_id=0,
                inode=0,
                content_key=f"legacy:{row['id']}:{row['path']}",
                last_seen_at=mediafile.c.discovered_at,
                status="present",
            )
        )

    with op.batch_alter_table("mediafile") as batch_op:
        batch_op.alter_column("device_id", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("inode", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column(
            "content_key",
            existing_type=sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        )
        batch_op.alter_column("last_seen_at", existing_type=sa.DateTime(), nullable=False)
        batch_op.alter_column(
            "status",
            existing_type=sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        )
        batch_op.create_unique_constraint("uq_mediafile_path", ["path"])
        batch_op.create_index("ix_mediafile_path", ["path"], unique=False)
        batch_op.create_index("ix_mediafile_content_key", ["content_key"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("mediafile") as batch_op:
        batch_op.drop_index("ix_mediafile_content_key")
        batch_op.drop_index("ix_mediafile_path")
        batch_op.drop_constraint("uq_mediafile_path", type_="unique")
        batch_op.drop_column("status")
        batch_op.drop_column("last_seen_at")
        batch_op.drop_column("content_key")
        batch_op.drop_column("inode")
        batch_op.drop_column("device_id")
