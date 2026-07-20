"""add resource reservations

Revision ID: 0013_resource_reservations
Revises: 0012_performance_workload_signature
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0013_resource_reservations"
down_revision: str | None = "0012_performance_workload_signature"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "resource_reservation" in set(inspector.get_table_names()):
        return
    op.create_table(
        "resource_reservation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("scheduler_session_id", sa.Integer(), nullable=True),
        sa.Column("resource_class", sa.String(), nullable=False),
        sa.Column("cpu_reserved", sa.Float(), nullable=True),
        sa.Column("memory_bytes_reserved", sa.Integer(), nullable=True),
        sa.Column("exclusive", sa.Boolean(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("release_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["attempt_id"], ["jobattempt.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"]),
        sa.ForeignKeyConstraint(["scheduler_session_id"], ["scheduler_session.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id"),
    )
    for name, columns, unique in (
        ("ix_resource_reservation_attempt_id", ["attempt_id"], True),
        ("ix_resource_reservation_job_id", ["job_id"], False),
        ("ix_resource_reservation_scheduler_session_id", ["scheduler_session_id"], False),
        ("ix_resource_reservation_resource_class", ["resource_class"], False),
        ("ix_resource_reservation_status", ["status"], False),
        ("ix_resource_reservation_released_at", ["released_at"], False),
    ):
        op.create_index(name, "resource_reservation", columns, unique=unique)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "resource_reservation" not in set(inspector.get_table_names()):
        return
    for name in (
        "ix_resource_reservation_released_at",
        "ix_resource_reservation_status",
        "ix_resource_reservation_resource_class",
        "ix_resource_reservation_scheduler_session_id",
        "ix_resource_reservation_job_id",
        "ix_resource_reservation_attempt_id",
    ):
        op.drop_index(name, table_name="resource_reservation")
    op.drop_table("resource_reservation")
