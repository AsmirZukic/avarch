"""add performance workload signature

Revision ID: 0012_performance_workload_signature
Revises: 0011_performance_environment_signature
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0012_performance_workload_signature"
down_revision: str | None = "0011_performance_environment_signature"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("workload_signature_hash", sqlmodel.sql.sqltypes.AutoString()),
    ("workload_signature_json", sqlmodel.sql.sqltypes.AutoString()),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("performance_observation")}
    missing = [(name, column_type) for name, column_type in _COLUMNS if name not in existing]
    if missing:
        with op.batch_alter_table("performance_observation") as batch:
            for name, column_type in missing:
                batch.add_column(sa.Column(name, column_type, nullable=True))
    _create_index_if_missing(
        inspector,
        table="performance_observation",
        name="ix_performance_observation_workload_signature_hash",
        columns=["workload_signature_hash"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("performance_observation")}
    if "ix_performance_observation_workload_signature_hash" in {
        index["name"] for index in inspector.get_indexes("performance_observation")
    }:
        op.drop_index(
            "ix_performance_observation_workload_signature_hash",
            table_name="performance_observation",
        )
    present = [name for name, _column_type in _COLUMNS if name in existing]
    if not present:
        return
    with op.batch_alter_table("performance_observation") as batch:
        for name in reversed(present):
            batch.drop_column(name)


def _create_index_if_missing(
    inspector: sa.Inspector,
    *,
    table: str,
    name: str,
    columns: list[str],
) -> None:
    if name in {index["name"] for index in inspector.get_indexes(table)}:
        return
    op.create_index(name, table, columns, unique=False)
