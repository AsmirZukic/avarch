"""reset queue for validation lifecycle

Revision ID: 1d4f0a7c9b2e
Revises: cc5e72c1a9d4
Create Date: 2026-06-15 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1d4f0a7c9b2e"
down_revision: str | None = "cc5e72c1a9d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "validationresult" in tables:
        op.execute(sa.text("DELETE FROM validationresult"))
    if "jobattempt" in tables:
        op.execute(sa.text("DELETE FROM jobattempt"))
    if "job" in tables:
        op.execute(sa.text("DELETE FROM job"))
    if "schedulerstate" in tables:
        op.execute(sa.text("DELETE FROM schedulerstate"))

    if "validationresult" not in tables:
        op.create_table(
            "validationresult",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("job_id", sa.Integer(), nullable=False),
            sa.Column("attempt_id", sa.Integer(), nullable=False),
            sa.Column("plan_hash", sa.String(), nullable=False),
            sa.Column("policy_hash", sa.String(), nullable=False),
            sa.Column("output_path", sa.String(), nullable=False),
            sa.Column("output_fs_fingerprint", sa.String(), nullable=True),
            sa.Column("passed", sa.Boolean(), nullable=False),
            sa.Column("details_json", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["attempt_id"], ["jobattempt.id"]),
            sa.ForeignKeyConstraint(["job_id"], ["job.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_validationresult_job_id", "validationresult", ["job_id"])
        op.create_index(
            "ix_validationresult_attempt_id",
            "validationresult",
            ["attempt_id"],
            unique=True,
        )
        op.create_index("ix_validationresult_plan_hash", "validationresult", ["plan_hash"])
        op.create_index("ix_validationresult_policy_hash", "validationresult", ["policy_hash"])
        op.create_index(
            "ix_validationresult_output_fs_fingerprint",
            "validationresult",
            ["output_fs_fingerprint"],
        )
        op.create_index("ix_validationresult_passed", "validationresult", ["passed"])

    job_columns = (
        {column["name"] for column in inspector.get_columns("job")} if "job" in tables else set()
    )
    job_indexes = (
        {index["name"] for index in inspector.get_indexes("job")} if "job" in tables else set()
    )
    if "latest_validation_id" not in job_columns:
        with op.batch_alter_table("job") as batch_op:
            batch_op.add_column(sa.Column("latest_validation_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_job_latest_validation_id_validationresult",
                "validationresult",
                ["latest_validation_id"],
                ["id"],
            )
    if "ix_job_latest_validation_id" not in job_indexes:
        op.create_index("ix_job_latest_validation_id", "job", ["latest_validation_id"])

    op.execute(
        sa.text(
            "INSERT INTO schedulerstate (id, paused, updated_at) "
            "VALUES (1, 0, CURRENT_TIMESTAMP)"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("job") as batch_op:
        batch_op.drop_constraint(
            "fk_job_latest_validation_id_validationresult",
            type_="foreignkey",
        )
        batch_op.drop_index("ix_job_latest_validation_id")
        batch_op.drop_column("latest_validation_id")

    op.drop_index("ix_validationresult_passed", table_name="validationresult")
    op.drop_index("ix_validationresult_output_fs_fingerprint", table_name="validationresult")
    op.drop_index("ix_validationresult_policy_hash", table_name="validationresult")
    op.drop_index("ix_validationresult_plan_hash", table_name="validationresult")
    op.drop_index("ix_validationresult_attempt_id", table_name="validationresult")
    op.drop_index("ix_validationresult_job_id", table_name="validationresult")
    op.drop_table("validationresult")
