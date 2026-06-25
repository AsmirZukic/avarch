from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlmodel import Session

from avarch.adapters.probe import build_probe_hash
from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import MediaFile, MediaFileStatus, ProbeResult
from avarch.adapters.sqlite.planning import load_planning_context
from avarch.adapters.sqlite.probes import store_probe_result
from avarch.application.planning import PlanningError, match_profile
from avarch.models.probe import NormalizedProbe, VideoStream
from avarch.profiles.models import EncodingProfile, ProfileDocument
from avarch.profiles.registry import ProfileOrigin, ResolvedProfile
from avarch.serialization import canonical_json


def test_context_loads_canonical_probe(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine, tmp_path / "movie.mkv")
    probe_result = _store_probe(engine, media_file)

    with Session(engine) as session:
        context = load_planning_context(
            session,
            input_path=Path(media_file.path),
            resolved_profile=_resolved_profile(),
        )

    assert context.media_file.id == media_file.id
    assert context.probe_result.id == probe_result.id
    assert context.normalized_probe.video_streams[0].codec == "hevc"
    assert context.profile_name == "av1_1080p_sdr"


def test_context_rejects_untracked_file(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    with Session(engine) as session, pytest.raises(PlanningError, match="not present"):
        load_planning_context(
            session,
            input_path=tmp_path / "movie.mkv",
            resolved_profile=_resolved_profile(),
        )


def test_context_rejects_missing_media_file(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(
        engine,
        tmp_path / "movie.mkv",
        status=MediaFileStatus.MISSING,
    )
    _store_probe(engine, media_file)

    with Session(engine) as session, pytest.raises(PlanningError, match="marked missing"):
        load_planning_context(
            session,
            input_path=Path(media_file.path),
            resolved_profile=_resolved_profile(),
        )


def test_context_rejects_null_latest_probe_id(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine, tmp_path / "movie.mkv")

    with Session(engine) as session, pytest.raises(PlanningError, match="No canonical probe"):
        load_planning_context(
            session,
            input_path=Path(media_file.path),
            resolved_profile=_resolved_profile(),
        )


def test_context_rejects_missing_referenced_probe(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine, tmp_path / "movie.mkv")
    _set_latest_probe_id_without_foreign_key(engine, media_file.id or 0, 999)

    with Session(engine) as session, pytest.raises(PlanningError, match="no longer exists"):
        load_planning_context(
            session,
            input_path=Path(media_file.path),
            resolved_profile=_resolved_profile(),
        )


def test_context_rejects_probe_owned_by_another_file(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    first = _insert_media_file(engine, tmp_path / "one.mkv", fingerprint="shared")
    second = _insert_media_file(engine, tmp_path / "two.mkv", fingerprint="shared")
    second_probe = _store_probe(engine, second)

    with Session(engine) as session:
        stored_first = session.get_one(MediaFile, first.id)
        stored_first.latest_probe_id = second_probe.id
        session.add(stored_first)
        session.commit()

    with Session(engine) as session, pytest.raises(PlanningError, match="another media file"):
        load_planning_context(
            session,
            input_path=Path(first.path),
            resolved_profile=_resolved_profile(),
        )


def test_context_rejects_stale_probe(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine, tmp_path / "movie.mkv")
    _store_probe(engine, media_file)

    with Session(engine) as session:
        stored_media_file = session.get_one(MediaFile, media_file.id)
        stored_media_file.fs_fingerprint = "changed"
        session.add(stored_media_file)
        session.commit()

    with (
        Session(engine) as session,
        pytest.raises(PlanningError, match="Run avarch probe for this file again"),
    ):
        load_planning_context(
            session,
            input_path=Path(media_file.path),
            resolved_profile=_resolved_profile(),
        )


def test_context_does_not_fallback_to_other_probe_rows(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine, tmp_path / "movie.mkv")
    stale = _insert_probe_result(engine, media_file, source_fs_fingerprint="old")
    _insert_probe_result(engine, media_file, source_fs_fingerprint=media_file.fs_fingerprint)
    _set_latest_probe_id(engine, media_file.id or 0, stale.id or 0)

    with (
        Session(engine) as session,
        pytest.raises(PlanningError, match="current filesystem fingerprint"),
    ):
        load_planning_context(
            session,
            input_path=Path(media_file.path),
            resolved_profile=_resolved_profile(),
        )


def test_profile_accepts_h264() -> None:
    result = match_profile(_profile(), _probe_with_video_codec("h264"))

    assert result.matched is True


def test_profile_accepts_hevc() -> None:
    result = match_profile(_profile(), _probe_with_video_codec("hevc"))

    assert result.matched is True


def test_profile_rejects_av1() -> None:
    result = match_profile(_profile(), _probe_with_video_codec("av1"))

    assert result.matched is False


def test_matching_is_case_insensitive() -> None:
    result = match_profile(_profile(), _probe_with_video_codec("AV1"))

    assert result.matched is False


def test_matching_rejects_missing_codec() -> None:
    result = match_profile(_profile(), _probe_with_video_codec(None))

    assert result.matched is False
    assert result.reasons


def test_rejection_contains_reason() -> None:
    result = match_profile(_profile(), _probe_with_video_codec("av1"))

    assert result.reasons == ("video codec is excluded: av1",)


def _engine(tmp_path: Path) -> Engine:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    return engine


def _insert_media_file(
    engine: Engine,
    path: Path,
    *,
    status: MediaFileStatus = MediaFileStatus.PRESENT,
    fingerprint: str = "fingerprint",
) -> MediaFile:
    path.write_bytes(b"media")
    media_file = MediaFile(
        path=str(path.resolve()),
        size_bytes=path.stat().st_size,
        mtime_ns=path.stat().st_mtime_ns,
        device_id=path.stat().st_dev,
        inode=path.stat().st_ino,
        fs_fingerprint=fingerprint,
        discovered_at=_now(),
        last_seen_at=_now(),
        status=status,
    )
    with Session(engine) as session:
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
    return media_file


def _store_probe(engine: Engine, media_file: MediaFile) -> ProbeResult:
    with Session(engine) as session:
        stored_media_file = session.get_one(MediaFile, media_file.id)
        probe_result = store_probe_result(
            session,
            media_file=stored_media_file,
            raw_probe={"format": {}},
            normalized_probe=_normalized_probe(),
            created_at=_now(),
        )
        session.commit()
        session.refresh(probe_result)
        return probe_result


def _insert_probe_result(
    engine: Engine,
    media_file: MediaFile,
    *,
    source_fs_fingerprint: str,
) -> ProbeResult:
    normalized_probe = _normalized_probe()
    probe_result = ProbeResult(
        media_file_id=media_file.id or 0,
        ffprobe_json="{}",
        normalized_json=canonical_json(normalized_probe),
        probe_hash=build_probe_hash(normalized_probe),
        source_fs_fingerprint=source_fs_fingerprint,
        created_at=_now(),
    )
    with Session(engine) as session:
        session.add(probe_result)
        session.commit()
        session.refresh(probe_result)
    return probe_result


def _set_latest_probe_id(engine: Engine, media_file_id: int, probe_result_id: int) -> None:
    with Session(engine) as session:
        media_file = session.get_one(MediaFile, media_file_id)
        media_file.latest_probe_id = probe_result_id
        session.add(media_file)
        session.commit()


def _set_latest_probe_id_without_foreign_key(
    engine: Engine,
    media_file_id: int,
    probe_result_id: int,
) -> None:
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(
            sa.text("UPDATE mediafile SET latest_probe_id = :probe_result_id WHERE id = :id"),
            {"id": media_file_id, "probe_result_id": probe_result_id},
        )
        connection.commit()


def _normalized_probe() -> NormalizedProbe:
    return NormalizedProbe(
        container="matroska,webm",
        video_streams=[VideoStream(index=0, codec="hevc", width=3840, height=2160)],
    )


def _profile() -> EncodingProfile:
    return _resolved_profile().profile


def _resolved_profile() -> ResolvedProfile:
    document = ProfileDocument.model_validate(
        {
            "schema_version": 1,
            "name": "av1_1080p_sdr",
            "backend": "av1an",
            "container": "mkv",
            "match": {"video_codec_not": ["av1"]},
            "video": {
                "max_width": 1920,
                "hdr_to_sdr": True,
                "source": "vapoursynth",
            },
            "av1an": {
                "encoder": "svt-av1",
                "workers": 2,
                "video_args": "--preset 6 --crf 28 --keyint 240 --lp 2",
            },
            "audio": {
                "codec": "libopus",
                "bitrate": "128k",
                "channels": 2,
                "languages": ["eng"],
            },
            "subtitles": {
                "languages": ["eng"],
                "keep_forced": True,
            },
        }
    )
    return ResolvedProfile(
        name=document.name,
        document=document,
        profile=document.encoding_profile(),
        origin=ProfileOrigin.USER,
        source="test",
    )


def _probe_with_video_codec(codec: str | None) -> NormalizedProbe:
    return NormalizedProbe(video_streams=[VideoStream(index=0, codec=codec)])


def _now() -> datetime:
    return datetime(2026, 6, 14, tzinfo=UTC)
