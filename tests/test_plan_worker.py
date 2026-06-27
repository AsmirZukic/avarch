from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session, select

from avarch.adapters.filesystem.plans import write_plan_artifacts
from avarch.adapters.filesystem.scanner import create_file_snapshot
from avarch.adapters.probe import normalize_probe
from avarch.adapters.scheduler_workers import execute_plan_job
from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.enqueue import SqliteEnqueueStore
from avarch.adapters.sqlite.models import Job, MediaFile, MediaFileStatus
from avarch.adapters.sqlite.planning import load_planning_context
from avarch.adapters.sqlite.probes import store_probe_result
from avarch.adapters.vapoursynth import generate_vapoursynth_script
from avarch.adapters.vpy_env import planning_runtime_identity_for_data_dir
from avarch.application.enqueue import enqueue_inventory
from avarch.application.planning import build_plan
from avarch.application.vapoursynth_identity import resolve_vapoursynth_template
from avarch.config import AppConfig
from avarch.domain.jobs import JobStage, JobStatus
from avarch.profiles.registry import ProfileRegistry
from tests.probe_fixtures import sdr_probe_payload


def test_plan_worker_reuses_existing_relative_artifact_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    database_url = f"sqlite:///{tmp_path / '.avarch' / 'avarch.adapters.sqlite.db'}"
    config = _config(database_url)
    (tmp_path / ".avarch").mkdir()
    engine = create_db_engine(database_url)
    create_db_schema(engine)
    now = datetime.now(UTC)
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"media")

    with Session(engine) as session:
        media_file = _add_media_file(session, source, now)
        _add_probe(session, media_file, now)
        context = load_planning_context(
            session,
            input_path=source,
            resolved_profile=ProfileRegistry.from_config(config).get("av1_1080p_sdr"),
        )
        template = resolve_vapoursynth_template(context.profile)
        data_dir = Path(".avarch")
        plan = build_plan(
            context,
            data_dir=data_dir,
            runtime_identity=planning_runtime_identity_for_data_dir(data_dir),
            resolved_template=template,
        )
        write_plan_artifacts(
            plan=plan,
            vapoursynth_script=generate_vapoursynth_script(plan, template=template),
        )
        enqueue_inventory(
            SqliteEnqueueStore(session),
            config=config,
            profile_name="av1_1080p_sdr",
            priority=0,
            now=now,
        )
        session.commit()
        job = session.exec(select(Job)).one()

    asyncio.run(execute_plan_job(job_id=job.id or 0, runner_id="runner", config=config))

    with Session(engine) as session:
        stored = session.get(Job, job.id)

    assert stored is not None
    assert stored.status == JobStatus.QUEUED
    assert stored.stage == JobStage.ENCODE
    assert stored.plan_hash == plan.plan_hash


def _add_media_file(session: Session, path: Path, now: datetime) -> MediaFile:
    snapshot = create_file_snapshot(path)
    media_file = MediaFile(
        path=str(snapshot.path),
        size_bytes=snapshot.size_bytes,
        mtime_ns=snapshot.mtime_ns,
        device_id=snapshot.device_id,
        inode=snapshot.inode,
        fs_fingerprint=snapshot.fs_fingerprint,
        discovered_at=now,
        last_seen_at=now,
        status=MediaFileStatus.PRESENT,
    )
    session.add(media_file)
    session.commit()
    session.refresh(media_file)
    return media_file


def _add_probe(session: Session, media_file: MediaFile, now: datetime) -> None:
    raw_probe = sdr_probe_payload()
    normalized = normalize_probe(raw_probe)
    store_probe_result(
        session,
        media_file=media_file,
        raw_probe=raw_probe,
        normalized_probe=normalized,
        created_at=now,
    )
    session.commit()
    session.refresh(media_file)


def _config(database_url: str) -> AppConfig:
    return AppConfig.model_validate(
        {
            "app": {"data_dir": ".avarch"},
            "database": {"url": database_url},
        }
    )
