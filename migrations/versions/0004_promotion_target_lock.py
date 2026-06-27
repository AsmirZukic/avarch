"""add promotion target lock key

Revision ID: 0004_promotion_target_lock
Revises: 0003_job_outcome_reason
Create Date: 2026-06-24 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0004_promotion_target_lock"
down_revision: str | None = "0003_job_outcome_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("promotionrecord")}
    if "promotion_target_path" not in columns:
        op.add_column(
            "promotionrecord",
            sa.Column("promotion_target_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        )
        op.create_index(
            op.f("ix_promotionrecord_promotion_target_path"),
            "promotionrecord",
            ["promotion_target_path"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("promotionrecord")}
    if "promotion_target_path" in columns:
        op.drop_index(
            op.f("ix_promotionrecord_promotion_target_path"),
            table_name="promotionrecord",
        )
        op.drop_column("promotionrecord", "promotion_target_path")