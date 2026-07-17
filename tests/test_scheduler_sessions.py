from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import SchedulerSession
from avarch.adapters.sqlite.scheduler_sessions import (
    current_scheduler_session,
    end_scheduler_session,
    start_scheduler_session,
)


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


def test_start_scheduler_session_is_idempotent_for_same_owner(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.sqlite'}")
    create_db_schema(engine)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    with Session(engine) as session, session.begin():
        first = start_scheduler_session(
            session,
            owner_id="runner-1",
            workspace_id="workspace-1",
            pid=1234,
            host="host",
            now=now,
        )
        second = start_scheduler_session(
            session,
            owner_id="runner-1",
            workspace_id="workspace-1",
            pid=1234,
            host="host",
            now=now + timedelta(seconds=1),
        )
        first_id = first.id
        second_id = second.id

    with Session(engine) as session:
        sessions = session.exec(select(SchedulerSession)).all()

    assert first_id == second_id
    assert len(sessions) == 1


def test_end_scheduler_session_records_reason_once(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.sqlite'}")
    create_db_schema(engine)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    with Session(engine) as session, session.begin():
        scheduler_session = start_scheduler_session(
            session,
            owner_id="runner-1",
            workspace_id="workspace-1",
            pid=1234,
            host="host",
            now=now,
        )
        session_id = scheduler_session.id or 0
        end_scheduler_session(session, session_id=session_id, now=now, reason="normal")
        end_scheduler_session(
            session,
            session_id=session_id,
            now=now + timedelta(seconds=1),
            reason="overwritten",
        )

    with Session(engine) as session:
        stored = session.get(SchedulerSession, session_id)

    assert stored is not None
    assert stored.ended_at == now.replace(tzinfo=None)
    assert stored.end_reason == "normal"


def test_next_scheduler_start_marks_stale_open_session_interrupted(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.sqlite'}")
    create_db_schema(engine)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    with Session(engine) as session, session.begin():
        stale = start_scheduler_session(
            session,
            owner_id="old-runner",
            workspace_id="workspace-1",
            pid=1111,
            host="host",
            now=now,
        )
        fresh = start_scheduler_session(
            session,
            owner_id="new-runner",
            workspace_id="workspace-1",
            pid=2222,
            host="host",
            now=now + timedelta(minutes=1),
        )
        stale_id = stale.id or 0
        fresh_id = fresh.id or 0

    with Session(engine) as session:
        stale_stored = session.get(SchedulerSession, stale_id)
        fresh_stored = session.get(SchedulerSession, fresh_id)
        current = current_scheduler_session(session, workspace_id="workspace-1")

    assert stale_stored is not None
    assert stale_stored.end_reason == "interrupted"
    assert stale_stored.ended_at == (now + timedelta(minutes=1)).replace(tzinfo=None)
    assert fresh_stored is not None
    assert fresh_stored.ended_at is None
    assert current is not None
    assert current.id == fresh_id
