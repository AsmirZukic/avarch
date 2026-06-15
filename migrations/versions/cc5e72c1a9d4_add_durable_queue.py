"""add durable queue

Revision ID: cc5e72c1a9d4
Revises: 7d56d2f55f31
Create Date: 2026-06-15 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cc5e72c1a9d4"
down_revision: str | None = "7d56d2f55f31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("media_file_id", sa.Integer(), nullable=False),
        sa.Column("profile_name", sa.String(), nullable=False),
        sa.Column("profile_hash", sa.String(), nullable=False),
        sa.Column("source_fs_fingerprint", sa.String(), nullable=False),
        sa.Column("queue_key", sa.String(), nullable=False),
        sa.Column("probe_result_id", sa.Integer(), nullable=True),
        sa.Column("probe_hash", sa.String(), nullable=True),
        sa.Column("plan_hash", sa.String(), nullable=True),
        sa.Column("plan_path", sa.String(), nullable=True),
        sa.Column("output_path", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claimed_by", sa.String(), nullable=True),
        sa.Column("last_error_type", sa.String(), nullable=True),
        sa.Column("last_error_message", sa.String(), nullable=True),
        sa.Column("skip_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["media_file_id"], ["mediafile.id"]),
        sa.ForeignKeyConstraint(["probe_result_id"], ["proberesult.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_media_file_id", "job", ["media_file_id"])
    op.create_index("ix_job_profile_name", "job", ["profile_name"])
    op.create_index("ix_job_probe_result_id", "job", ["probe_result_id"])
    op.create_index("ix_job_status", "job", ["status"])
    op.create_index("ix_job_stage", "job", ["stage"])
    op.create_index("ix_job_priority", "job", ["priority"])
    op.create_index("ix_job_claimed_by", "job", ["claimed_by"])
    op.create_index("ix_job_queue_key", "job", ["queue_key"], unique=True)
    op.create_index("ix_job_plan_hash", "job", ["plan_hash"], unique=True)

    op.create_table(
        "jobattempt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("resource_class", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("runner_id", sa.String(), nullable=False),
        sa.Column("command_json", sa.String(), nullable=True),
        sa.Column("details_json", sa.String(), nullable=True),
        sa.Column("stdout_log", sa.String(), nullable=True),
        sa.Column("stderr_log", sa.String(), nullable=True),
        sa.Column("temp_dir", sa.String(), nullable=True),
        sa.Column("output_path", sa.String(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_number"),
    )
    op.create_index("ix_jobattempt_job_id", "jobattempt", ["job_id"])
    op.create_index("ix_jobattempt_stage", "jobattempt", ["stage"])
    op.create_index("ix_jobattempt_resource_class", "jobattempt", ["resource_class"])
    op.create_index("ix_jobattempt_status", "jobattempt", ["status"])

    op.create_table(
        "schedulerstate",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("runner_id", sa.String(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_schedulerstate_runner_id", "schedulerstate", ["runner_id"])

    op.execute(
        sa.text(
            "INSERT INTO schedulerstate (id, paused, updated_at) "
            "VALUES (1, 0, CURRENT_TIMESTAMP)"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_schedulerstate_runner_id", table_name="schedulerstate")
    op.drop_table("schedulerstate")

    op.drop_index("ix_jobattempt_status", table_name="jobattempt")
    op.drop_index("ix_jobattempt_resource_class", table_name="jobattempt")
    op.drop_index("ix_jobattempt_stage", table_name="jobattempt")
    op.drop_index("ix_jobattempt_job_id", table_name="jobattempt")
    op.drop_table("jobattempt")

    op.drop_index("ix_job_plan_hash", table_name="job")
    op.drop_index("ix_job_queue_key", table_name="job")
    op.drop_index("ix_job_claimed_by", table_name="job")
    op.drop_index("ix_job_priority", table_name="job")
    op.drop_index("ix_job_stage", table_name="job")
    op.drop_index("ix_job_status", table_name="job")
    op.drop_index("ix_job_probe_result_id", table_name="job")
    op.drop_index("ix_job_profile_name", table_name="job")
    op.drop_index("ix_job_media_file_id", table_name="job")
    op.drop_table("job")
