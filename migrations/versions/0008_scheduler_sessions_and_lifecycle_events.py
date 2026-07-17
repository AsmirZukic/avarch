"""add scheduler sessions and lifecycle event metadata

Revision ID: 0008_scheduler_sessions_and_lifecycle_events
Revises: 0007_structured_attempt_progress
Create Date: 2026-07-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0008_scheduler_sessions_and_lifecycle_events"
down_revision: str | None = "0007_structured_attempt_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "scheduler_session" not in tables:
        op.create_table(
            "scheduler_session",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("owner_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("pid", sa.Integer(), nullable=True),
            sa.Column("host", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("ended_at", sa.DateTime(), nullable=True),
            sa.Column("end_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_scheduler_session_owner_id",
            "scheduler_session",
            ["owner_id"],
            unique=False,
        )
        op.create_index(
            "ix_scheduler_session_pid",
            "scheduler_session",
            ["pid"],
            unique=False,
        )
        op.create_index(
            "ix_scheduler_session_ended_at",
            "scheduler_session",
            ["ended_at"],
            unique=False,
        )

    jobattempt_columns = {column["name"] for column in inspector.get_columns("jobattempt")}
    jobattempt_fk_exists = _foreign_key_exists(
        inspector,
        table="jobattempt",
        local_cols=["scheduler_session_id"],
        remote_table="scheduler_session",
    )
    if "scheduler_session_id" not in jobattempt_columns or not jobattempt_fk_exists:
        with op.batch_alter_table("jobattempt") as batch:
            if "scheduler_session_id" not in jobattempt_columns:
                batch.add_column(sa.Column("scheduler_session_id", sa.Integer(), nullable=True))
            if not jobattempt_fk_exists:
                batch.create_foreign_key(
                    "fk_jobattempt_scheduler_session_id_scheduler_session",
                    "scheduler_session",
                    ["scheduler_session_id"],
                    ["id"],
                )
    _create_index_if_missing(
        inspector,
        table="jobattempt",
        name="ix_jobattempt_scheduler_session_id",
        columns=["scheduler_session_id"],
    )

    inspector = sa.inspect(bind)
    jobevent_columns = {column["name"] for column in inspector.get_columns("jobevent")}
    jobevent_attempt_fk_exists = _foreign_key_exists(
        inspector,
        table="jobevent",
        local_cols=["attempt_id"],
        remote_table="jobattempt",
    )
    jobevent_session_fk_exists = _foreign_key_exists(
        inspector,
        table="jobevent",
        local_cols=["scheduler_session_id"],
        remote_table="scheduler_session",
    )
    if (
        {"attempt_id", "scheduler_session_id", "stage", "dedupe_key"} - jobevent_columns
        or not jobevent_attempt_fk_exists
        or not jobevent_session_fk_exists
    ):
        with op.batch_alter_table("jobevent") as batch:
            if "attempt_id" not in jobevent_columns:
                batch.add_column(sa.Column("attempt_id", sa.Integer(), nullable=True))
            if "scheduler_session_id" not in jobevent_columns:
                batch.add_column(sa.Column("scheduler_session_id", sa.Integer(), nullable=True))
            if "stage" not in jobevent_columns:
                batch.add_column(sa.Column("stage", sa.String(), nullable=True))
            if "dedupe_key" not in jobevent_columns:
                batch.add_column(
                    sa.Column("dedupe_key", sqlmodel.sql.sqltypes.AutoString(), nullable=True)
                )
            if not jobevent_attempt_fk_exists:
                batch.create_foreign_key(
                    "fk_jobevent_attempt_id_jobattempt",
                    "jobattempt",
                    ["attempt_id"],
                    ["id"],
                )
            if not jobevent_session_fk_exists:
                batch.create_foreign_key(
                    "fk_jobevent_scheduler_session_id_scheduler_session",
                    "scheduler_session",
                    ["scheduler_session_id"],
                    ["id"],
                )
    inspector = sa.inspect(bind)
    for name, columns in (
        ("ix_jobevent_attempt_id", ["attempt_id"]),
        ("ix_jobevent_scheduler_session_id", ["scheduler_session_id"]),
        ("ix_jobevent_dedupe_key", ["dedupe_key"]),
    ):
        _create_index_if_missing(inspector, table="jobevent", name=name, columns=columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, index_name in (
        ("jobevent", "ix_jobevent_dedupe_key"),
        ("jobevent", "ix_jobevent_scheduler_session_id"),
        ("jobevent", "ix_jobevent_attempt_id"),
        ("jobattempt", "ix_jobattempt_scheduler_session_id"),
    ):
        if index_name in {index["name"] for index in inspector.get_indexes(table)}:
            op.drop_index(index_name, table_name=table)
    jobevent_existing = {column["name"] for column in inspector.get_columns("jobevent")}
    if {"dedupe_key", "stage", "scheduler_session_id", "attempt_id"} & jobevent_existing:
        jobevent_fks = {
            key["name"]
            for key in inspector.get_foreign_keys("jobevent")
            if key["name"] is not None
        }
        with op.batch_alter_table("jobevent") as batch:
            for name in (
                "fk_jobevent_scheduler_session_id_scheduler_session",
                "fk_jobevent_attempt_id_jobattempt",
            ):
                if name in jobevent_fks:
                    batch.drop_constraint(name, type_="foreignkey")
            for column in ("dedupe_key", "stage", "scheduler_session_id", "attempt_id"):
                if column in jobevent_existing:
                    batch.drop_column(column)

    jobattempt_existing = {column["name"] for column in inspector.get_columns("jobattempt")}
    if "scheduler_session_id" in jobattempt_existing:
        jobattempt_fks = {
            key["name"]
            for key in inspector.get_foreign_keys("jobattempt")
            if key["name"] is not None
        }
        with op.batch_alter_table("jobattempt") as batch:
            if "fk_jobattempt_scheduler_session_id_scheduler_session" in jobattempt_fks:
                batch.drop_constraint(
                    "fk_jobattempt_scheduler_session_id_scheduler_session",
                    type_="foreignkey",
                )
            batch.drop_column("scheduler_session_id")
    if "scheduler_session" in set(inspector.get_table_names()):
        op.drop_table("scheduler_session")


def _create_index_if_missing(
    inspector: sa.Inspector,
    *,
    table: str,
    name: str,
    columns: list[str],
) -> None:
    existing = {index["name"] for index in inspector.get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns, unique=False)


def _foreign_key_exists(
    inspector: sa.Inspector,
    *,
    table: str,
    local_cols: list[str],
    remote_table: str,
) -> bool:
    return any(
        key["constrained_columns"] == local_cols and key["referred_table"] == remote_table
        for key in inspector.get_foreign_keys(table)
    )
