"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-15 16:49:26.484403

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "appmeta",
        sa.Column("key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("value", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "job",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("media_file_id", sa.Integer(), nullable=False),
        sa.Column("profile_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("profile_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_fs_fingerprint", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("queue_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("probe_result_id", sa.Integer(), nullable=True),
        sa.Column("probe_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("plan_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("plan_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("output_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("latest_validation_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claimed_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("last_error_type", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("last_error_message", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("skip_reason", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["latest_validation_id"],
            ["validationresult.id"],
        ),
        sa.ForeignKeyConstraint(
            ["media_file_id"],
            ["mediafile.id"],
        ),
        sa.ForeignKeyConstraint(
            ["probe_result_id"],
            ["proberesult.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_job_claimed_by"), "job", ["claimed_by"], unique=False)
    op.create_index(
        op.f("ix_job_latest_validation_id"),
        "job",
        ["latest_validation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_media_file_id"),
        "job",
        ["media_file_id"],
        unique=False,
    )
    op.create_index(op.f("ix_job_plan_hash"), "job", ["plan_hash"], unique=True)
    op.create_index(op.f("ix_job_priority"), "job", ["priority"], unique=False)
    op.create_index(
        op.f("ix_job_probe_result_id"),
        "job",
        ["probe_result_id"],
        unique=False,
    )
    op.create_index(op.f("ix_job_profile_name"), "job", ["profile_name"], unique=False)
    op.create_index(op.f("ix_job_queue_key"), "job", ["queue_key"], unique=True)
    op.create_index(op.f("ix_job_stage"), "job", ["stage"], unique=False)
    op.create_index(op.f("ix_job_status"), "job", ["status"], unique=False)
    op.create_table(
        "jobattempt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("resource_class", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("runner_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("command_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("details_json", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("stdout_log", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("stderr_log", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("temp_dir", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("output_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("error_type", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("error_message", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_number"),
    )
    op.create_index(
        op.f("ix_jobattempt_job_id"),
        "jobattempt",
        ["job_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_jobattempt_resource_class"),
        "jobattempt",
        ["resource_class"],
        unique=False,
    )
    op.create_index(op.f("ix_jobattempt_stage"), "jobattempt", ["stage"], unique=False)
    op.create_index(op.f("ix_jobattempt_status"), "jobattempt", ["status"], unique=False)
    op.create_table(
        "mediafile",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mtime_ns", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("inode", sa.Integer(), nullable=False),
        sa.Column("fs_fingerprint", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("discovered_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("latest_probe_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["latest_probe_id"],
            ["proberesult.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mediafile_fs_fingerprint"),
        "mediafile",
        ["fs_fingerprint"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mediafile_latest_probe_id"),
        "mediafile",
        ["latest_probe_id"],
        unique=False,
    )
    op.create_index(op.f("ix_mediafile_path"), "mediafile", ["path"], unique=True)
    op.create_table(
        "proberesult",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("media_file_id", sa.Integer(), nullable=False),
        sa.Column("ffprobe_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("normalized_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("probe_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_fs_fingerprint", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["media_file_id"],
            ["mediafile.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_proberesult_media_file_id"),
        "proberesult",
        ["media_file_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_proberesult_probe_hash"),
        "proberesult",
        ["probe_hash"],
        unique=False,
    )
    op.create_table(
        "schedulerstate",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("runner_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_schedulerstate_runner_id"),
        "schedulerstate",
        ["runner_id"],
        unique=False,
    )
    op.create_table(
        "validationresult",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("plan_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("policy_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("output_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("output_fs_fingerprint", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("details_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["jobattempt.id"],
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_validationresult_attempt_id"),
        "validationresult",
        ["attempt_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_validationresult_job_id"),
        "validationresult",
        ["job_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_validationresult_output_fs_fingerprint"),
        "validationresult",
        ["output_fs_fingerprint"],
        unique=False,
    )
    op.create_index(
        op.f("ix_validationresult_passed"),
        "validationresult",
        ["passed"],
        unique=False,
    )
    op.create_index(
        op.f("ix_validationresult_plan_hash"),
        "validationresult",
        ["plan_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_validationresult_policy_hash"),
        "validationresult",
        ["policy_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_validationresult_policy_hash"), table_name="validationresult")
    op.drop_index(op.f("ix_validationresult_plan_hash"), table_name="validationresult")
    op.drop_index(op.f("ix_validationresult_passed"), table_name="validationresult")
    op.drop_index(
        op.f("ix_validationresult_output_fs_fingerprint"),
        table_name="validationresult",
    )
    op.drop_index(op.f("ix_validationresult_job_id"), table_name="validationresult")
    op.drop_index(op.f("ix_validationresult_attempt_id"), table_name="validationresult")
    op.drop_table("validationresult")
    op.drop_index(op.f("ix_schedulerstate_runner_id"), table_name="schedulerstate")
    op.drop_table("schedulerstate")
    op.drop_index(op.f("ix_proberesult_probe_hash"), table_name="proberesult")
    op.drop_index(op.f("ix_proberesult_media_file_id"), table_name="proberesult")
    op.drop_table("proberesult")
    op.drop_index(op.f("ix_mediafile_path"), table_name="mediafile")
    op.drop_index(op.f("ix_mediafile_latest_probe_id"), table_name="mediafile")
    op.drop_index(op.f("ix_mediafile_fs_fingerprint"), table_name="mediafile")
    op.drop_table("mediafile")
    op.drop_index(op.f("ix_jobattempt_status"), table_name="jobattempt")
    op.drop_index(op.f("ix_jobattempt_stage"), table_name="jobattempt")
    op.drop_index(op.f("ix_jobattempt_resource_class"), table_name="jobattempt")
    op.drop_index(op.f("ix_jobattempt_job_id"), table_name="jobattempt")
    op.drop_table("jobattempt")
    op.drop_index(op.f("ix_job_status"), table_name="job")
    op.drop_index(op.f("ix_job_stage"), table_name="job")
    op.drop_index(op.f("ix_job_queue_key"), table_name="job")
    op.drop_index(op.f("ix_job_profile_name"), table_name="job")
    op.drop_index(op.f("ix_job_probe_result_id"), table_name="job")
    op.drop_index(op.f("ix_job_priority"), table_name="job")
    op.drop_index(op.f("ix_job_plan_hash"), table_name="job")
    op.drop_index(op.f("ix_job_media_file_id"), table_name="job")
    op.drop_index(op.f("ix_job_latest_validation_id"), table_name="job")
    op.drop_index(op.f("ix_job_claimed_by"), table_name="job")
    op.drop_table("job")
    op.drop_table("appmeta")
