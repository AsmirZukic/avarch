from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from avarch.config import AppConfig, EncodingProfile
from avarch.models.db import MediaFile, MediaFileStatus, ProbeResult
from avarch.models.plan import (
    AudioPlan,
    Av1anCommandSpec,
    PlanArtifactPaths,
    SubtitlePlan,
    SubtitleStreamPlan,
    TranscodePlan,
    ValidationPolicy,
    VapourSynthPlan,
    VideoPlan,
)
from avarch.models.probe import NormalizedProbe, SubtitleStream, VideoStream
from avarch.probe import ProbeError, parse_normalized_probe_json
from avarch.serialization import canonical_json
from avarch.vapoursynth import (
    GENERATOR_VERSION,
    ResolvedVapourSynthTemplate,
    build_vapoursynth_identity_hash,
    validate_script_syntax,
)


class PlanningError(RuntimeError):
    pass


class PlanArtifactConflictError(PlanningError):
    pass


@dataclass(frozen=True, slots=True)
class PlanningContext:
    media_file: MediaFile
    probe_result: ProbeResult
    normalized_probe: NormalizedProbe
    profile_name: str
    profile: EncodingProfile


@dataclass(frozen=True, slots=True)
class ProfileMatchResult:
    matched: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TargetDimensions:
    width: int
    height: int
    resize_required: bool


def parse_encoder_args(value: str) -> list[str]:
    return shlex.split(value)


def build_profile_hash(
    profile: EncodingProfile,
    *,
    template_hash: str | None = None,
) -> str:
    profile_payload = profile.model_dump(
        mode="json",
        exclude={"vapoursynth_template"},
    )
    profile_payload["vapoursynth_template_hash"] = template_hash
    payload = b"profile-v2\0" + canonical_json(profile_payload).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def build_work_key(
    *,
    input_path: Path,
    source_fs_fingerprint: str,
    probe_hash: str,
    profile_hash: str,
    vapoursynth_identity_hash: str,
) -> str:
    payload_json = canonical_json(
        {
            "input_path": str(input_path.resolve()),
            "source_fs_fingerprint": source_fs_fingerprint,
            "probe_hash": probe_hash,
            "profile_hash": profile_hash,
            "vapoursynth_identity_hash": vapoursynth_identity_hash,
        }
    )
    payload = b"work-v2\0" + payload_json.encode("utf-8")
    return hashlib.blake2b(payload, digest_size=20).hexdigest()


def build_plan_hash_payload(plan: TranscodePlan) -> dict[str, Any]:
    payload_data = plan.model_dump(mode="json")
    payload_data.pop("plan_hash", None)
    return payload_data


def build_plan_hash(plan: TranscodePlan) -> str:
    payload = b"plan-v1\0" + canonical_json(build_plan_hash_payload(plan)).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def finalize_plan_hash(plan: TranscodePlan) -> TranscodePlan:
    return plan.model_copy(update={"plan_hash": build_plan_hash(plan)})


def load_planning_context(
    session: Session,
    *,
    input_path: Path,
    profile_name: str,
    config: AppConfig,
) -> PlanningContext:
    profile = config.profiles.get(profile_name)
    if profile is None:
        raise PlanningError(f"Unknown profile: {profile_name}")

    media_file = session.exec(
        select(MediaFile).where(MediaFile.path == str(input_path.resolve()))
    ).first()
    if media_file is None:
        raise PlanningError("File is not present in the media inventory.")
    if _status_value(media_file.status) == MediaFileStatus.MISSING.value:
        raise PlanningError("File is marked missing in the media inventory.")
    if media_file.latest_probe_id is None:
        raise PlanningError("No canonical probe result exists for this file.")

    probe_result = session.get(ProbeResult, media_file.latest_probe_id)
    if probe_result is None:
        raise PlanningError("The canonical probe result no longer exists.")
    if probe_result.media_file_id != media_file.id:
        raise PlanningError("The canonical probe belongs to another media file.")
    if probe_result.source_fs_fingerprint is None:
        raise PlanningError("The canonical probe has no source filesystem fingerprint.")
    if probe_result.source_fs_fingerprint != media_file.fs_fingerprint:
        raise PlanningError(
            "The canonical probe does not match the current filesystem fingerprint.\n"
            "Run avarch probe for this file again."
        )

    try:
        normalized_probe = parse_normalized_probe_json(probe_result.normalized_json)
    except ProbeError as exc:
        raise PlanningError(str(exc)) from exc
    if not normalized_probe.video_streams:
        raise PlanningError("The canonical probe contains no video stream.")

    return PlanningContext(
        media_file=media_file,
        probe_result=probe_result,
        normalized_probe=normalized_probe,
        profile_name=profile_name,
        profile=profile,
    )


def _status_value(status: MediaFileStatus | str) -> str:
    if isinstance(status, MediaFileStatus):
        return status.value
    return status


def match_profile(
    profile: EncodingProfile,
    probe: NormalizedProbe,
) -> ProfileMatchResult:
    if not probe.video_streams:
        return ProfileMatchResult(False, ("no video stream",))

    primary_video = min(probe.video_streams, key=lambda stream: stream.index)
    if primary_video.codec is None:
        return ProfileMatchResult(False, ("primary video codec is missing",))

    source_codec = primary_video.codec.strip().lower()
    blocked_codecs = {codec.strip().lower() for codec in profile.match.video_codec_not}
    if source_codec in blocked_codecs:
        return ProfileMatchResult(False, (f"video codec is excluded: {source_codec}",))

    return ProfileMatchResult(True, ())


def select_video(
    probe: NormalizedProbe,
    profile: EncodingProfile,
) -> VideoPlan:
    if not probe.video_streams:
        raise PlanningError("The canonical probe contains no video stream.")

    stream = _primary_video(probe.video_streams)
    if stream.codec is None:
        raise PlanningError("Primary video codec is missing.")
    if stream.width is None or stream.height is None:
        raise PlanningError("Primary video dimensions are missing.")

    dimensions = calculate_target_dimensions(
        source_width=stream.width,
        source_height=stream.height,
        max_width=profile.video.max_width,
    )

    return VideoPlan(
        source_stream_index=stream.index,
        source_codec=stream.codec,
        source_width=stream.width,
        source_height=stream.height,
        source_bit_depth=stream.bit_depth,
        source_pix_fmt=stream.pix_fmt,
        source_color_transfer=stream.color_transfer,
        source_color_primaries=stream.color_primaries,
        source_color_space=stream.color_space,
        source_hdr_metadata_present=stream.hdr_metadata_present,
        max_width=profile.video.max_width,
        target_width=dimensions.width,
        target_height=dimensions.height,
        resize_required=dimensions.resize_required,
        hdr_to_sdr=profile.video.hdr_to_sdr,
        source=profile.video.source,
    )


def calculate_target_dimensions(
    *,
    source_width: int,
    source_height: int,
    max_width: int,
) -> TargetDimensions:
    if source_width <= 0:
        raise PlanningError("Source width must be positive.")
    if source_height <= 0:
        raise PlanningError("Source height must be positive.")
    if max_width <= 0:
        raise PlanningError("Profile max_width must be positive.")
    if max_width % 2 != 0:
        raise PlanningError("Profile max_width must be even.")

    candidate_width = min(source_width, max_width)
    target_width = candidate_width - candidate_width % 2
    if target_width < 2:
        raise PlanningError("Target width must be at least 2.")

    if target_width == source_width:
        target_height = source_height - source_height % 2
    else:
        numerator = source_height * target_width
        target_height = ((numerator + source_width) // (2 * source_width)) * 2

    if target_height < 2:
        raise PlanningError("Target height must be at least 2.")

    return TargetDimensions(
        width=target_width,
        height=target_height,
        resize_required=target_width != source_width or target_height != source_height,
    )


def select_audio(
    probe: NormalizedProbe,
    profile: EncodingProfile,
) -> AudioPlan:
    language_rank = {
        language: index
        for index, language in enumerate(_normalized_languages(profile.audio.languages))
    }
    eligible = [
        stream
        for stream in probe.audio_streams
        if stream.language is not None and _normalize_language(stream.language) in language_rank
    ]
    if not eligible:
        raise PlanningError("No eligible audio stream matches the selected profile.")

    stream = min(
        eligible,
        key=lambda item: (
            language_rank[_normalize_language(item.language or "")],
            item.commentary,
            not item.default,
            item.index,
        ),
    )
    return AudioPlan(
        source_stream_index=stream.index,
        source_codec=stream.codec,
        source_language=stream.language,
        source_channels=stream.channels,
        source_title=stream.title,
        source_commentary=stream.commentary,
        target_codec=profile.audio.codec,
        target_bitrate=profile.audio.bitrate,
        target_channels=profile.audio.channels,
    )


def select_subtitles(
    probe: NormalizedProbe,
    profile: EncodingProfile,
) -> SubtitlePlan:
    configured_languages = set(_normalized_languages(profile.subtitles.languages))
    streams = [
        _subtitle_plan(stream)
        for stream in sorted(probe.subtitle_streams, key=lambda item: item.index)
        if _subtitle_selected(
            stream,
            configured_languages,
            keep_forced=profile.subtitles.keep_forced,
        )
    ]
    return SubtitlePlan(streams=streams)


def build_plan(
    context: PlanningContext,
    *,
    data_dir: Path,
    resolved_template: ResolvedVapourSynthTemplate | None = None,
    generator_version: int = GENERATOR_VERSION,
) -> TranscodePlan:
    match = match_profile(context.profile, context.normalized_probe)
    if not match.matched:
        reasons = ", ".join(match.reasons)
        raise PlanningError(f"Profile does not apply to this file: {reasons}")

    if context.media_file.id is None:
        raise PlanningError("Media file must be persisted before planning.")

    if context.profile.vapoursynth_template is None and resolved_template is not None:
        raise PlanningError("Resolved template was provided for a profile without a template.")
    if context.profile.vapoursynth_template is not None and resolved_template is None:
        raise PlanningError("Profile requires a resolved VapourSynth template.")
    if (
        context.profile.vapoursynth_template is not None
        and resolved_template is not None
        and context.profile.vapoursynth_template != resolved_template.path
    ):
        raise PlanningError("Resolved template path does not match the selected profile.")

    mode = "custom_template" if resolved_template is not None else "generated"
    template_hash = resolved_template.template_hash if resolved_template is not None else None
    vapoursynth_identity_hash = build_vapoursynth_identity_hash(
        generator_version=generator_version,
        mode=mode,
        output_format="YUV420P10",
        resize_filter="spline36",
        template_hash=template_hash,
    )
    profile_hash = build_profile_hash(context.profile, template_hash=template_hash)
    input_path = Path(context.media_file.path).resolve()
    source_fs_fingerprint = context.media_file.fs_fingerprint
    work_key = build_work_key(
        input_path=input_path,
        source_fs_fingerprint=source_fs_fingerprint,
        probe_hash=context.probe_result.probe_hash,
        profile_hash=profile_hash,
        vapoursynth_identity_hash=vapoursynth_identity_hash,
    )
    paths = build_plan_paths(data_dir=data_dir, work_key=work_key, input_path=input_path)
    video = select_video(context.normalized_probe, context.profile)
    audio = select_audio(context.normalized_probe, context.profile)
    subtitles = select_subtitles(context.normalized_probe, context.profile)

    plan = TranscodePlan(
        plan_hash="",
        input_path=input_path,
        output_path=paths.output_path,
        temp_dir=paths.temp_dir,
        media_file_id=context.media_file.id,
        source_fs_fingerprint=source_fs_fingerprint,
        profile_name=context.profile_name,
        profile_hash=profile_hash,
        probe_hash=context.probe_result.probe_hash,
        video=video,
        audio=audio,
        subtitles=subtitles,
        vapoursynth=VapourSynthPlan(
            generator_version=generator_version,
            mode=mode,
            script_path=paths.artifacts.vapoursynth_script,
            source_path=input_path,
            source_stream_index=video.source_stream_index,
            index_cache_dir=paths.index_cache_dir,
            target_width=video.target_width,
            target_height=video.target_height,
            output_format="YUV420P10",
            resize_filter="spline36",
            source_pix_fmt=video.source_pix_fmt,
            source_color_transfer=video.source_color_transfer,
            source_color_primaries=video.source_color_primaries,
            source_color_space=video.source_color_space,
            source_hdr_metadata_present=video.source_hdr_metadata_present,
            hdr_to_sdr=video.hdr_to_sdr,
            template_path=resolved_template.path if resolved_template is not None else None,
            template_hash=template_hash,
            identity_hash=vapoursynth_identity_hash,
        ),
        av1an=Av1anCommandSpec(
            input_path=paths.artifacts.vapoursynth_script,
            output_path=paths.output_path,
            temp_dir=paths.temp_dir,
            encoder=context.profile.av1an.encoder,
            encoder_args=parse_encoder_args(context.profile.av1an.video_args),
            workers=context.profile.av1an.workers,
        ),
        validation=ValidationPolicy(
            expected_container=context.profile.container,
            maximum_width=context.profile.video.max_width,
            expected_audio_codec=context.profile.audio.codec,
            expected_audio_channels=context.profile.audio.channels,
            expected_subtitle_streams=[
                stream.source_stream_index for stream in subtitles.streams
            ],
            source_duration_seconds=context.normalized_probe.duration_seconds,
        ),
        artifacts=paths.artifacts,
    )
    return finalize_plan_hash(plan)


@dataclass(frozen=True, slots=True)
class DerivedPlanPaths:
    artifacts: PlanArtifactPaths
    output_path: Path
    temp_dir: Path
    index_cache_dir: Path


def build_plan_paths(
    *,
    data_dir: Path,
    work_key: str,
    input_path: Path,
) -> DerivedPlanPaths:
    artifact_dir = data_dir / "plans" / work_key
    work_dir = data_dir / "work" / work_key
    stem = input_path.stem
    output_path = work_dir / f"{stem}.av1.mkv"
    temp_dir = work_dir / "av1an"
    if output_path.resolve() == input_path.resolve():
        raise PlanningError("Planned output path would overwrite the input path.")

    return DerivedPlanPaths(
        artifacts=PlanArtifactPaths(
            artifact_dir=artifact_dir,
            plan_json=artifact_dir / "plan.json",
            vapoursynth_script=artifact_dir / f"{stem}.vpy",
            av1an_command_json=artifact_dir / "av1an.command.json",
            validation_policy_json=artifact_dir / "validation-policy.json",
        ),
        output_path=output_path,
        temp_dir=temp_dir,
        index_cache_dir=work_dir / "bestsource",
    )


def write_plan_artifacts(
    *,
    plan: TranscodePlan,
    vapoursynth_script: str,
) -> PlanArtifactPaths:
    validate_script_syntax(vapoursynth_script)
    paths = plan.artifacts
    artifact_dir = paths.artifact_dir
    parent = artifact_dir.parent
    parent.mkdir(parents=True, exist_ok=True)

    artifact_payloads = _artifact_payloads(plan, vapoursynth_script=vapoursynth_script)
    if artifact_dir.exists():
        if _artifact_dir_matches(artifact_payloads, artifact_dir):
            return paths
        raise PlanArtifactConflictError(f"Plan artifact bundle already exists: {artifact_dir}")

    temp_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{artifact_dir.name}.tmp-",
            dir=parent,
        )
    )
    try:
        for relative_path, content in artifact_payloads.items():
            path = temp_dir / relative_path
            with path.open("w", encoding="utf-8", newline="\n") as output_file:
                output_file.write(content)
                output_file.flush()
                os.fsync(output_file.fileno())
        os.replace(temp_dir, artifact_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return paths


def _artifact_payloads(
    plan: TranscodePlan,
    *,
    vapoursynth_script: str,
) -> dict[str, str]:
    return {
        "plan.json": canonical_json(plan) + "\n",
        plan.artifacts.vapoursynth_script.name: vapoursynth_script,
        "av1an.command.json": canonical_json(plan.av1an) + "\n",
        "validation-policy.json": canonical_json(plan.validation) + "\n",
    }


def _artifact_dir_matches(payloads: dict[str, str], artifact_dir: Path) -> bool:
    if not artifact_dir.is_dir():
        return False
    if {path.name for path in artifact_dir.iterdir()} != set(payloads):
        return False
    for relative_path, expected_content in payloads.items():
        path = artifact_dir / relative_path
        if not path.is_file():
            return False
        if path.read_text(encoding="utf-8") != expected_content:
            return False
    return True


def _primary_video(streams: list[VideoStream]) -> VideoStream:
    return min(streams, key=lambda stream: stream.index)


def _subtitle_plan(stream: SubtitleStream) -> SubtitleStreamPlan:
    return SubtitleStreamPlan(
        source_stream_index=stream.index,
        codec=stream.codec,
        language=stream.language,
        title=stream.title,
        forced=stream.forced,
    )


def _subtitle_selected(
    stream: SubtitleStream,
    configured_languages: set[str],
    *,
    keep_forced: bool,
) -> bool:
    if stream.language is not None and _normalize_language(stream.language) in configured_languages:
        return True
    return keep_forced and stream.forced


def _normalized_languages(languages: list[str]) -> list[str]:
    return [_normalize_language(language) for language in languages]


def _normalize_language(language: str) -> str:
    return language.strip().lower()
