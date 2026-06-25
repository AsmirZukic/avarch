from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from avarch.adapters.probe import build_probe_hash, normalize_probe
from avarch.adapters.sqlite.models import MediaFile, MediaFileStatus, ProbeResult
from avarch.application.planning import (
    PlanningContext,
    add_sdr_color_encoder_args,
    build_plan,
    build_plan_hash_payload,
    build_work_key,
)
from avarch.profiles.models import ProfileDocument
from avarch.profiles.registry import ProfileOrigin, ResolvedProfile
from avarch.serialization import canonical_json
from tests.probe_fixtures import sdr_probe_payload


def test_build_plan_copies_probe_pixel_and_color_metadata(tmp_path: Path) -> None:
    context = _context(tmp_path)

    plan = build_plan(context, data_dir=tmp_path / ".avarch")

    assert plan.video.source_pix_fmt == "yuv420p10le"
    assert plan.video.source_bit_depth == 10
    assert plan.video.source_color_transfer == "bt709"
    assert plan.video.source_color_primaries == "bt709"
    assert plan.video.source_color_space == "bt709"
    assert plan.video.source_hdr_metadata_present is False
    assert plan.vapoursynth.source_pix_fmt == plan.video.source_pix_fmt
    assert plan.vapoursynth.source_color_transfer == plan.video.source_color_transfer


def test_build_plan_tags_sdr_av1_output_color(tmp_path: Path) -> None:
    plan = build_plan(_context(tmp_path), data_dir=tmp_path / ".avarch")

    assert plan.av1an.encoder_args[-10:] == [
        "--color-primaries",
        "1",
        "--transfer-characteristics",
        "1",
        "--matrix-coefficients",
        "1",
        "--color-range",
        "0",
        "--chroma-sample-position",
        "1",
    ]


def test_sdr_color_encoder_args_preserve_user_options() -> None:
    arguments = add_sdr_color_encoder_args(
        [
            "--preset",
            "6",
            "--color-primaries=9",
            "--transfer-characteristics",
            "18",
        ]
    )

    assert arguments == [
        "--preset",
        "6",
        "--color-primaries=9",
        "--transfer-characteristics",
        "18",
        "--matrix-coefficients",
        "1",
        "--color-range",
        "0",
        "--chroma-sample-position",
        "1",
    ]


def test_build_plan_populates_target_dimensions(tmp_path: Path) -> None:
    plan = build_plan(_context(tmp_path), data_dir=tmp_path / ".avarch")

    assert plan.video.target_width == 1920
    assert plan.video.target_height == 1080
    assert plan.video.resize_required is True
    assert plan.vapoursynth.target_width == 1920
    assert plan.vapoursynth.target_height == 1080


def test_plan_hash_payload_excludes_plan_hash(tmp_path: Path) -> None:
    plan = build_plan(_context(tmp_path), data_dir=tmp_path / ".avarch")

    payload = build_plan_hash_payload(plan)

    assert "plan_hash" not in payload
    assert "vapoursynth" in payload


def test_plan_hash_payload_includes_promotion_policy(tmp_path: Path) -> None:
    plan = build_plan(_context(tmp_path), data_dir=tmp_path / ".avarch")

    payload = build_plan_hash_payload(plan)

    assert payload["promotion"]["policy_hash"] == plan.promotion.policy_hash


def test_work_key_uses_promotion_policy_hash(tmp_path: Path) -> None:
    first = build_work_key(
        input_path=tmp_path / "movie.mkv",
        source_fs_fingerprint="source-fs",
        probe_hash="probe",
        profile_hash="profile",
        vapoursynth_identity_hash="vpy",
        execution_identity_hash="execution",
        promotion_policy_hash="one",
    )
    second = build_work_key(
        input_path=tmp_path / "movie.mkv",
        source_fs_fingerprint="source-fs",
        probe_hash="probe",
        profile_hash="profile",
        vapoursynth_identity_hash="vpy",
        execution_identity_hash="execution",
        promotion_policy_hash="two",
    )

    assert first != second


def test_build_plan_supports_custom_filter_profile(tmp_path: Path) -> None:
    scripts_dir = tmp_path / ".avarch" / "scripts"
    scripts_dir.mkdir(parents=True)
    filter_path = scripts_dir / "my_filter.py"
    filter_path.write_text("def apply(video, context):\n    return video\n", encoding="utf-8")
    context = _context(tmp_path, resolved_profile=_resolved_filter_profile(filter_path))

    plan = build_plan(context, data_dir=tmp_path / ".avarch")

    assert plan.vapoursynth.mode == "custom_filter"
    assert plan.vapoursynth.filter_path == plan.artifacts.artifact_dir / "vpy" / "user_filter.py"
    assert plan.vapoursynth.filter_hash
    assert plan.vapoursynth.filter_entrypoint == "apply"


def _context(
    tmp_path: Path,
    *,
    resolved_profile: ResolvedProfile | None = None,
) -> PlanningContext:
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"media")
    normalized = normalize_probe(sdr_probe_payload())
    probe_hash = build_probe_hash(normalized)
    now = datetime(2026, 6, 14, tzinfo=UTC)
    media_file = MediaFile(
        id=1,
        path=str(path.resolve()),
        size_bytes=5,
        mtime_ns=1,
        device_id=1,
        inode=1,
        fs_fingerprint="fs",
        discovered_at=now,
        last_seen_at=now,
        status=MediaFileStatus.PRESENT,
        latest_probe_id=1,
    )
    probe_result = ProbeResult(
        id=1,
        media_file_id=1,
        ffprobe_json="{}",
        normalized_json=canonical_json(normalized),
        probe_hash=probe_hash,
        source_fs_fingerprint="fs",
        created_at=now,
    )
    resolved_profile = resolved_profile or _resolved_profile()
    return PlanningContext(
        media_file=media_file,
        probe_result=probe_result,
        normalized_probe=normalized,
        profile_name=resolved_profile.name,
        profile=resolved_profile.profile,
    )


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


def _resolved_filter_profile(filter_path: Path) -> ResolvedProfile:
    document = ProfileDocument.model_validate(
        {
            "schema_version": 1,
            "name": "filtered",
            "backend": "av1an",
            "container": "mkv",
            "vapoursynth": {
                "mode": "custom_filter",
                "script": filter_path,
                "entrypoint": "apply",
                "api_version": 1,
            },
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
