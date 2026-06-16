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

from avarch.contracts import (
    EXECUTION_IDENTITY_HASH_CONTRACT,
    PLAN_HASH_CONTRACT,
    PROFILE_HASH_CONTRACT,
    PROMOTION_POLICY_HASH_CONTRACT,
    VALIDATION_POLICY_HASH_CONTRACT,
    WORK_KEY_CONTRACT,
)
from avarch.models.db import MediaFile, MediaFileStatus, ProbeResult
from avarch.models.plan import (
    AV1AN_COMMAND_CONTRACT_VERSION,
    FFMPEG_MUX_CONTRACT_VERSION,
    AudioPlan,
    Av1anCommandSpec,
    ExecutionIdentity,
    ExecutionRuntimePaths,
    FfmpegMuxSpec,
    PlanArtifactPaths,
    SubtitlePlan,
    SubtitleStreamPlan,
    TranscodePlan,
    VapourSynthPlan,
    VideoPlan,
)
from avarch.models.probe import NormalizedProbe, SubtitleStream, VideoStream
from avarch.models.promotion import PromotionPolicy
from avarch.models.validation import (
    DecodeSamplePolicy,
    ExpectedSubtitlePolicy,
    ValidationPolicy,
)
from avarch.probe import ProbeError, parse_normalized_probe_json
from avarch.profiles.models import EncodingProfile
from avarch.profiles.registry import ResolvedProfile
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


SUPPORTED_AV1AN_VERSION_FAMILY = "0.5.x"
ACCEPTED_CONTAINER_NAMES = {
    "mkv": ["matroska,webm"],
}
EXPECTED_AUDIO_CODEC_NAMES = {
    "libopus": "opus",
}


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
    payload = f"{PROFILE_HASH_CONTRACT}\0".encode() + canonical_json(profile_payload).encode(
        "utf-8"
    )
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def build_work_key(
    *,
    input_path: Path,
    source_fs_fingerprint: str,
    probe_hash: str,
    profile_hash: str,
    vapoursynth_identity_hash: str,
    execution_identity_hash: str,
    promotion_policy_hash: str,
) -> str:
    payload_json = canonical_json(
        {
            "input_path": str(input_path.resolve()),
            "source_fs_fingerprint": source_fs_fingerprint,
            "probe_hash": probe_hash,
            "profile_hash": profile_hash,
            "vapoursynth_identity_hash": vapoursynth_identity_hash,
            "execution_identity_hash": execution_identity_hash,
            "promotion_policy_hash": promotion_policy_hash,
        }
    )
    payload = f"{WORK_KEY_CONTRACT}\0".encode() + payload_json.encode("utf-8")
    return hashlib.blake2b(payload, digest_size=20).hexdigest()


def build_execution_identity() -> ExecutionIdentity:
    identity = ExecutionIdentity(
        av1an_contract_version=AV1AN_COMMAND_CONTRACT_VERSION,
        ffmpeg_mux_contract_version=FFMPEG_MUX_CONTRACT_VERSION,
        av1an_version_family=SUPPORTED_AV1AN_VERSION_FAMILY,
        video_container="mkv",
        final_container="mkv",
        identity_hash="",
    )
    return identity.model_copy(update={"identity_hash": build_execution_identity_hash(identity)})


def build_execution_identity_hash(identity: ExecutionIdentity) -> str:
    payload = identity.model_dump(mode="json")
    payload.pop("identity_hash", None)
    payload["command_policy"] = {
        "av1an_no_defaults": True,
        "av1an_concat_method": "ffmpeg",
        "av1an_pixel_format": "yuv420p10le",
        "av1an_cache_mode": "temp",
        "av1an_overwrite_policy": "never-overwrite",
        "av1an_temporary_state_retention": "keep",
        "ffmpeg_mux_policy": {
            "copy_video": True,
            "copy_selected_subtitles": True,
            "copy_chapters": True,
            "copy_global_metadata": True,
            "overwrite_policy": "never",
            "atomic_final_placement": True,
        },
    }
    data = f"{EXECUTION_IDENTITY_HASH_CONTRACT}\0".encode() + canonical_json(payload).encode(
        "utf-8"
    )
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def build_plan_hash_payload(plan: TranscodePlan) -> dict[str, Any]:
    payload_data = plan.model_dump(mode="json")
    payload_data.pop("plan_hash", None)
    return payload_data


def build_plan_hash(plan: TranscodePlan) -> str:
    payload = f"{PLAN_HASH_CONTRACT}\0".encode() + canonical_json(
        build_plan_hash_payload(plan)
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def finalize_plan_hash(plan: TranscodePlan) -> TranscodePlan:
    return plan.model_copy(update={"plan_hash": build_plan_hash(plan)})


def build_validation_policy_hash(policy: ValidationPolicy) -> str:
    payload_data = policy.model_dump(mode="json")
    payload_data.pop("policy_hash", None)
    payload = f"{VALIDATION_POLICY_HASH_CONTRACT}\0".encode() + canonical_json(
        payload_data
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def finalize_validation_policy(policy: ValidationPolicy) -> ValidationPolicy:
    return policy.model_copy(update={"policy_hash": build_validation_policy_hash(policy)})


def build_promotion_policy_hash(policy: PromotionPolicy) -> str:
    payload_data = policy.model_dump(mode="json")
    payload_data.pop("policy_hash", None)
    payload = f"{PROMOTION_POLICY_HASH_CONTRACT}\0".encode() + canonical_json(
        payload_data
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def finalize_promotion_policy(policy: PromotionPolicy) -> PromotionPolicy:
    return policy.model_copy(update={"policy_hash": build_promotion_policy_hash(policy)})


def load_planning_context(
    session: Session,
    *,
    input_path: Path,
    resolved_profile: ResolvedProfile,
) -> PlanningContext:
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
        profile_name=resolved_profile.name,
        profile=resolved_profile.profile,
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


def build_validation_policy(
    *,
    probe: NormalizedProbe,
    profile: EncodingProfile,
    video: VideoPlan,
    audio: AudioPlan,
    subtitles: SubtitlePlan,
    source_size_bytes: int,
) -> ValidationPolicy:
    if probe.duration_seconds is None or probe.duration_seconds <= 0:
        raise PlanningError("Source duration is missing or invalid.")
    accepted_containers = ACCEPTED_CONTAINER_NAMES.get(profile.container)
    if accepted_containers is None:
        raise PlanningError(f"Unsupported output container for validation: {profile.container}")
    expected_audio_codec = EXPECTED_AUDIO_CODEC_NAMES.get(audio.target_codec)
    if expected_audio_codec is None:
        raise PlanningError(f"Unsupported audio encoder for validation: {audio.target_codec}")

    policy = ValidationPolicy(
        policy_hash="",
        accepted_container_names=accepted_containers,
        source_duration_seconds=probe.duration_seconds,
        source_size_bytes=source_size_bytes,
        duration_tolerance_seconds=profile.validation.duration_tolerance_seconds,
        expected_video_stream_count=1,
        expected_video_codec="av1",
        expected_width=video.target_width,
        expected_height=video.target_height,
        expected_audio_stream_count=1,
        expected_audio_codec=expected_audio_codec,
        expected_audio_channels=audio.target_channels,
        expected_audio_language=audio.source_language,
        expected_subtitles=[
            ExpectedSubtitlePolicy(
                output_order=order,
                source_stream_index=stream.source_stream_index,
                codec=stream.codec,
                language=stream.language,
                forced=stream.forced,
            )
            for order, stream in enumerate(subtitles.streams)
        ],
        minimum_output_bytes=profile.validation.minimum_output_bytes,
        minimum_output_source_ratio=profile.validation.minimum_output_source_ratio,
        minimum_size_reduction_percent=profile.validation.minimum_size_reduction_percent,
        decode_sample=DecodeSamplePolicy(
            enabled=profile.validation.decode_sample,
            duration_seconds=profile.validation.decode_sample_seconds,
        ),
    )
    return finalize_validation_policy(policy)


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
    execution_identity = build_execution_identity()
    promotion_policy = finalize_promotion_policy(PromotionPolicy(policy_hash=""))
    input_path = Path(context.media_file.path).resolve()
    source_fs_fingerprint = context.media_file.fs_fingerprint
    work_key = build_work_key(
        input_path=input_path,
        source_fs_fingerprint=source_fs_fingerprint,
        probe_hash=context.probe_result.probe_hash,
        profile_hash=profile_hash,
        vapoursynth_identity_hash=vapoursynth_identity_hash,
        execution_identity_hash=execution_identity.identity_hash,
        promotion_policy_hash=promotion_policy.policy_hash,
    )
    paths = build_plan_paths(data_dir=data_dir, work_key=work_key, input_path=input_path)
    video = select_video(context.normalized_probe, context.profile)
    audio = select_audio(context.normalized_probe, context.profile)
    subtitles = select_subtitles(context.normalized_probe, context.profile)

    plan = TranscodePlan(
        plan_hash="",
        input_path=input_path,
        output_path=paths.output_path,
        temp_dir=paths.work_dir,
        media_file_id=context.media_file.id,
        source_fs_fingerprint=source_fs_fingerprint,
        profile_name=context.profile_name,
        profile_hash=profile_hash,
        probe_hash=context.probe_result.probe_hash,
        video=video,
        audio=audio,
        subtitles=subtitles,
        execution_identity=execution_identity,
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
            video_output_path=paths.video_output_path,
            temp_dir=paths.av1an_temp_dir,
            working_directory=paths.work_dir,
            encoder=context.profile.av1an.encoder,
            encoder_args=parse_encoder_args(context.profile.av1an.video_args),
            workers=context.profile.av1an.workers,
        ),
        mux=FfmpegMuxSpec(
            video_input_path=paths.video_output_path,
            source_input_path=input_path,
            output_path=paths.output_path,
            audio_stream_index=audio.source_stream_index,
            subtitle_stream_indexes=[stream.source_stream_index for stream in subtitles.streams],
            audio_codec=audio.target_codec,
            audio_bitrate=audio.target_bitrate,
            audio_channels=audio.target_channels,
        ),
        runtime=ExecutionRuntimePaths(
            runtime_dir=paths.runtime_dir,
            av1an_stdout_log=paths.runtime_dir / "av1an.stdout.log",
            av1an_stderr_log=paths.runtime_dir / "av1an.stderr.log",
            mux_stdout_log=paths.runtime_dir / "mux.stdout.log",
            mux_stderr_log=paths.runtime_dir / "mux.stderr.log",
            av1an_stage_marker=paths.runtime_dir / "av1an-stage.json",
            encode_result=paths.runtime_dir / "encode-result.json",
            validation_report=paths.runtime_dir / "validation-report.json",
            validation_decode_stdout_log=paths.runtime_dir / "validation.decode.stdout.log",
            validation_decode_stderr_log=paths.runtime_dir / "validation.decode.stderr.log",
        ),
        validation=build_validation_policy(
            probe=context.normalized_probe,
            profile=context.profile,
            video=video,
            audio=audio,
            subtitles=subtitles,
            source_size_bytes=context.media_file.size_bytes,
        ),
        promotion=promotion_policy,
        artifacts=paths.artifacts,
    )
    return finalize_plan_hash(plan)


@dataclass(frozen=True, slots=True)
class DerivedPlanPaths:
    artifacts: PlanArtifactPaths
    work_dir: Path
    output_path: Path
    video_output_path: Path
    av1an_temp_dir: Path
    runtime_dir: Path
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
    video_output_path = work_dir / "video-only.mkv"
    av1an_temp_dir = work_dir / "av1an"
    runtime_dir = work_dir / "runtime"
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
        work_dir=work_dir,
        output_path=output_path,
        video_output_path=video_output_path,
        av1an_temp_dir=av1an_temp_dir,
        runtime_dir=runtime_dir,
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
