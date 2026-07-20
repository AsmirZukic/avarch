"""add calibration observations

Revision ID: 0014_calibration_observations
Revises: 0013_resource_reservations
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0014_calibration_observations"
down_revision: str | None = "0013_resource_reservations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "calibration_observation" in set(inspector.get_table_names()):
        return
    op.create_table(
        "calibration_observation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("calibration_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("semantic_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("resource_policy_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column(
            "environment_signature_hash",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
        ),
        sa.Column("environment_signature_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("workload_signature_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workload_signature_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("sample_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("candidates_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("measurements_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("winner_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("measurement_cost_seconds", sa.Float(), nullable=False),
        sa.Column("incomplete", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("calibration_key"),
    )
    for name, columns, unique in (
        ("ix_calibration_observation_calibration_key", ["calibration_key"], True),
        ("ix_calibration_observation_semantic_hash", ["semantic_hash"], False),
        ("ix_calibration_observation_resource_policy_hash", ["resource_policy_hash"], False),
        (
            "ix_calibration_observation_environment_signature_hash",
            ["environment_signature_hash"],
            False,
        ),
        (
            "ix_calibration_observation_workload_signature_hash",
            ["workload_signature_hash"],
            False,
        ),
        ("ix_calibration_observation_status", ["status"], False),
        ("ix_calibration_observation_incomplete", ["incomplete"], False),
    ):
        op.create_index(name, "calibration_observation", columns, unique=unique)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "calibration_observation" not in set(inspector.get_table_names()):
        return
    for name in (
        "ix_calibration_observation_incomplete",
        "ix_calibration_observation_status",
        "ix_calibration_observation_workload_signature_hash",
        "ix_calibration_observation_environment_signature_hash",
        "ix_calibration_observation_resource_policy_hash",
        "ix_calibration_observation_semantic_hash",
        "ix_calibration_observation_calibration_key",
    ):
        op.drop_index(name, table_name="calibration_observation")
    op.drop_table("calibration_observation")
