"""Persist per-job authority and promotion recovery evidence.

Legacy jobs intentionally receive no replacement approval.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_promotion_safety"
down_revision: str | None = "0009_scheduler_runtime_capacity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = {
    "job": [("promotion_approval_json", sa.Text(), True, None)],
    "promotionrecord": [
        ("staging_stat_json", sa.Text(), True, None),
        ("original_digest", sa.String(), True, None),
        ("replacement_evidence_json", sa.Text(), True, None),
        ("delete_backup_approved", sa.Boolean(), False, sa.false()),
    ],
}


def upgrade() -> None:
    for table, columns in _COLUMNS.items():
        existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
        with op.batch_alter_table(table) as batch:
            for name, kind, nullable, default in columns:
                if name not in existing:
                    batch.add_column(
                        sa.Column(name, kind, nullable=nullable, server_default=default)
                    )


def downgrade() -> None:
    for table, columns in reversed(list(_COLUMNS.items())):
        existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
        with op.batch_alter_table(table) as batch:
            for name, *_ in reversed(columns):
                if name in existing:
                    batch.drop_column(name)
