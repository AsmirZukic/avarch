# Reuse the existing migration fixtures, including legacy row shapes.
# pyright: reportPrivateUsage=false
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import command

from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.migrations import upgrade_database
from tests.test_migrations import (
    _alembic_config,
    _insert,
    _job_values,
    _media_values,
    _probe_values,
)


def test_upgrade_does_not_grant_replacement_authority_to_legacy_jobs(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'old.db'}"
    command.upgrade(_alembic_config(url), "0009_scheduler_runtime_capacity")
    engine = create_db_engine(url)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        media = _insert(connection, "mediafile", _media_values(now))
        probe = _insert(connection, "proberesult", _probe_values(media, now))
        job_id = _insert(connection, "job", _job_values(media, probe, now))
    upgrade_database(url)
    with engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT promotion_approval_json FROM job WHERE id=:id"), {"id": job_id}
            ).scalar_one()
            is None
        )
    columns = {column["name"] for column in sa.inspect(engine).get_columns("promotionrecord")}
    assert {
        "original_digest",
        "staging_stat_json",
        "replacement_evidence_json",
        "delete_backup_approved",
    } <= columns
    command.downgrade(_alembic_config(url), "0009_scheduler_runtime_capacity")
    upgrade_database(url)
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM job")).scalar_one() == 1
