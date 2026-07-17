from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import SchedulerSession


def test_scheduler_session_round_trips_lifecycle_metadata(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.sqlite'}")
    create_db_schema(engine)
    started_at = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    ended_at = started_at + timedelta(minutes=5)

    with Session(engine) as session:
        session.add(
            SchedulerSession(
                owner_id="runner-1",
                workspace_id="workspace-1",
                pid=1234,
                host="build-host",
                started_at=started_at,
                ended_at=ended_at,
                end_reason="normal",
            )
        )
        session.commit()

        stored = session.exec(select(SchedulerSession)).one()

    assert stored.owner_id == "runner-1"
    assert stored.workspace_id == "workspace-1"
    assert stored.pid == 1234
    assert stored.host == "build-host"
    assert stored.started_at == started_at.replace(tzinfo=None)
    assert stored.ended_at == ended_at.replace(tzinfo=None)
    assert stored.end_reason == "normal"
