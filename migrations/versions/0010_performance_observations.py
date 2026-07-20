"""add performance observations

Revision ID: 0010_performance_observations
Revises: 0009_scheduler_runtime_capacity
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0010_performance_observations"
down_revision: str | None = "0009_scheduler_runtime_capacity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "performance_observation" in set(inspector.get_table_names()):
        return

    op.create_table(
        "performance_observation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("plan_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("semantic_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("resource_policy_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("resource_decision_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("tool_versions_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("total_frames", sa.Integer(), nullable=True),
        sa.Column("observation_duration_seconds", sa.Float(), nullable=True),
        sa.Column("aggregate_fps", sa.Float(), nullable=True),
        sa.Column("peak_rss_bytes", sa.Integer(), nullable=True),
        sa.Column("peak_cgroup_memory_bytes", sa.Integer(), nullable=True),
        sa.Column("average_cpu_utilization_percent", sa.Float(), nullable=True),
        sa.Column("swap_current_bytes_delta", sa.Integer(), nullable=True),
        sa.Column("cpu_throttled_events_delta", sa.Integer(), nullable=True),
        sa.Column("cpu_throttled_usec_delta", sa.Integer(), nullable=True),
        sa.Column("memory_oom_events_delta", sa.Integer(), nullable=True),
        sa.Column("memory_oom_kill_events_delta", sa.Integer(), nullable=True),
        sa.Column("resource_attribution_available", sa.Boolean(), nullable=False),
        sa.Column("incomplete", sa.Boolean(), nullable=False),
        sa.Column("progress_samples_observed", sa.Integer(), nullable=False),
        sa.Column("resource_samples_observed", sa.Integer(), nullable=False),
        sa.Column("warmup_seconds", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["jobattempt.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id"),
    )
    op.create_index(
        "ix_performance_observation_attempt_id",
        "performance_observation",
        ["attempt_id"],
        unique=True,
    )
    op.create_index(
        "ix_performance_observation_incomplete",
        "performance_observation",
        ["incomplete"],
        unique=False,
    )
    op.create_index(
        "ix_performance_observation_job_id",
        "performance_observation",
        ["job_id"],
        unique=False,
    )
    op.create_index(
        "ix_performance_observation_plan_hash",
        "performance_observation",
        ["plan_hash"],
        unique=False,
    )
    op.create_index(
        "ix_performance_observation_resource_policy_hash",
        "performance_observation",
        ["resource_policy_hash"],
        unique=False,
    )
    op.create_index(
        "ix_performance_observation_semantic_hash",
        "performance_observation",
        ["semantic_hash"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "performance_observation" not in set(inspector.get_table_names()):
        return
    op.drop_index("ix_performance_observation_semantic_hash", table_name="performance_observation")
    op.drop_index(
        "ix_performance_observation_resource_policy_hash",
        table_name="performance_observation",
    )
    op.drop_index("ix_performance_observation_plan_hash", table_name="performance_observation")
    op.drop_index("ix_performance_observation_job_id", table_name="performance_observation")
    op.drop_index("ix_performance_observation_incomplete", table_name="performance_observation")
    op.drop_index("ix_performance_observation_attempt_id", table_name="performance_observation")
    op.drop_table("performance_observation")
