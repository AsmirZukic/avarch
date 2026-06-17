from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from avarch.config import AppConfig, DatabaseSettings
from avarch.db import create_db_engine, create_db_schema
from avarch.models.db import Job, MediaFile, MediaFileStatus, ProbeResult
from avarch.tui.backend import LocalTuiBackend
from avarch.tui.models.workflow import CandidateState


def test_successful_analysis_refreshes_candidates(tmp_path: Path) -> None:
    backend, first_id, _second_id = _backend_with_media(tmp_path)

    before = _run(backend.list_workflow_candidates((tmp_path,)))
    assert before.rows[0].state == CandidateState.NEEDS_ANALYSIS

    summary = _run(backend.analyze_media((first_id,)))

    after = _run(backend.list_workflow_candidates((tmp_path,)))
    assert summary.completed == 1
    assert summary.failed == 0
    assert after.rows[0].state == CandidateState.READY
    assert after.rows[0].reason == "Video is HEVC."


def test_analysis_creates_no_jobs(tmp_path: Path) -> None:
    backend, first_id, second_id = _backend_with_media(tmp_path)

    _run(backend.analyze_media((first_id, second_id)))

    engine = create_db_engine(backend.database_url)
    with Session(engine) as session:
        jobs = list(session.exec(select(Job)).all())
        probes = list(session.exec(select(ProbeResult)).all())

    assert jobs == []
    assert len(probes) == 2


def test_partial_failure_keeps_successful_results(tmp_path: Path) -> None:
    backend, first_id, second_id = _backend_with_media(
        tmp_path,
        failing_name="second.mkv",
    )

    summary = _run(backend.analyze_media((first_id, second_id)))

    engine = create_db_engine(backend.database_url)
    with Session(engine) as session:
        media_files = list(session.exec(select(MediaFile).order_by(MediaFile.path)).all())

    assert summary.completed == 1
    assert summary.failed == 1
    assert "ffprobe failed" in summary.failures[0].error
    assert media_files[0].latest_probe_id is not None
    assert media_files[1].latest_probe_id is None


def test_analysis_does_not_encode(tmp_path: Path) -> None:
    calls: list[Path] = []

    def probe_runner(path: Path) -> Mapping[str, Any]:
        calls.append(path)
        return _sdr_probe_payload()

    backend, first_id, _second_id = _backend_with_media(tmp_path, probe_runner=probe_runner)

    _run(backend.analyze_media((first_id,)))

    assert calls == [tmp_path / "first.mkv"]


def _backend_with_media(
    tmp_path: Path,
    *,
    failing_name: str | None = None,
    probe_runner: Callable[[Path], Mapping[str, Any]] | None = None,
) -> tuple[LocalTuiBackend, int, int]:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"
    engine = create_db_engine(database_url)
    create_db_schema(engine)
    first = tmp_path / "first.mkv"
    second = tmp_path / "second.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    now = datetime.now(UTC)

    with Session(engine) as session:
        first_media = _add_media_file(session, first, now)
        second_media = _add_media_file(session, second, now)
        session.commit()
        session.refresh(first_media)
        session.refresh(second_media)
        first_id = first_media.id or 0
        second_id = second_media.id or 0

    def default_probe_runner(path: Path) -> Mapping[str, Any]:
        if failing_name is not None and path.name == failing_name:
            raise RuntimeError("ffprobe failed")
        return _sdr_probe_payload()

    runner = probe_runner or default_probe_runner
    backend = LocalTuiBackend(
        config=AppConfig(database=DatabaseSettings(url=database_url)),
        config_path=tmp_path / "avarch.toml",
        probe_runner=runner,
    )
    return backend, first_id, second_id


def _add_media_file(session: Session, path: Path, now: datetime) -> MediaFile:
    stat = path.stat()
    media_file = MediaFile(
        path=str(path),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        device_id=stat.st_dev,
        inode=stat.st_ino,
        fs_fingerprint=f"fingerprint-{path.name}",
        discovered_at=now,
        last_seen_at=now,
        status=MediaFileStatus.PRESENT,
    )
    session.add(media_file)
    session.flush()
    return media_file


def _sdr_probe_payload() -> dict[str, Any]:
    return {
        "streams": [
            {
                "index": 0,
                "codec_name": "hevc",
                "codec_type": "video",
                "width": 3840,
                "height": 2160,
                "pix_fmt": "yuv420p10le",
                "avg_frame_rate": "24000/1001",
                "r_frame_rate": "24000/1001",
                "bits_per_raw_sample": "10",
                "color_space": "bt709",
                "color_transfer": "bt709",
                "color_primaries": "bt709",
            },
            {
                "index": 1,
                "codec_name": "eac3",
                "codec_type": "audio",
                "channels": 6,
                "tags": {"language": "eng", "title": "Main Audio"},
                "disposition": {"default": 1, "forced": 0},
            },
        ],
        "chapters": [],
        "format": {
            "format_name": "matroska,webm",
            "duration": "600.000000",
            "bit_rate": "8123456",
        },
    }


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)
