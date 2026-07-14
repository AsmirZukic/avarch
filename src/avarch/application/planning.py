from __future__ import annotations

import hashlib
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from avarch.application.vapoursynth_identity import (
    GENERATOR_VERSION,
    ResolvedVapourSynthFilter,
    ResolvedVapourSynthTemplate,
    build_vapoursynth_identity_hash,
    resolve_vapoursynth_filter,
)
from avarch.contracts import (
    EXECUTION_IDENTITY_HASH_CONTRACT,
    PLAN_HASH_CONTRACT,
    PROFILE_HASH_CONTRACT,
    PROMOTION_POLICY_HASH_CONTRACT,
    VALIDATION_POLICY_HASH_CONTRACT,
    WORK_KEY_CONTRACT,
)
from avarch.domain.planning import (
    TargetDimensionError,
    TargetDimensions,
)
from avarch.domain.planning import (
    calculate_target_dimensions as calculate_domain_target_dimensions,
)
from avarch.domain.profiles import (
    ProfileMatchFacts,
    ProfileMatchPolicy,
    ProfileMatchResult,
    evaluate_profile_match,
)
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
    VapourSynthMode,
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
from avarch.profiles.models import EncodingProfile
from avarch.serialization import canonical_json


class PlanningError(RuntimeError):
    pass


class ProfileNotApplicableError(PlanningError):
    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        reason_text = ", ".join(self.reasons)
        super().__init__(f"Profile does not apply to this file: {reason_text}")


@dataclass(frozen=True, slots=True)
class PlanningRuntimeIdentity:
    manifest_hash: str
    avarch_image_digest: str
    python_version: str
    python_abi: str
    platform: str
    vapoursynth_version: str
    environment_id: str


@dataclass(frozen=True, slots=True)
class PlanningContext:
    media_file: PlanningMediaFile
    probe_result: PlanningProbeResult
    normalized_probe: NormalizedProbe
    profile_name: str
    profile: EncodingProfile
    relative_path_root: Path | None = None


class PlanningMediaFile(Protocol):
    id: int | None
    path: str
    size_bytes: int
    fs_fingerprint: str


class PlanningProbeResult(Protocol):
    id: int | None
    probe_hash: str


class PlanningProfile(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def profile(self) -> EncodingProfile: ...


@dataclass(frozen=True, slots=True)
class PlanningInputFile:
    id: int | None
    path: str
    size_bytes: int
    fs_fingerprint: str
    probe_state: str


@dataclass(frozen=True, slots=True)
class PlanningInputSelection:
    selected: tuple[PlanningInputFile, ...]
    missing: tuple[Path, ...]


class PlanningStore(Protocol):
    def select_inputs(
        self,
        *,
        file_selectors: Sequence[Path],
        workspace_root: Path,
        resolve_path: Callable[[Path], Path],
    ) -> PlanningInputSelection: ...

    def eligible_inputs(self) -> list[PlanningInputFile]: ...

    def load_context(
        self,
        *,
        input_path: Path,
        resolved_profile: PlanningProfile,
    ) -> PlanningContext: ...

    def equivalent_current_plan_exists(
        self,
        *,
        media_file_id: int | None,
        probe_hash: str,
        source_fs_fingerprint: str,
        profile_hash: str,
        execution_identity_hash: str,
    ) -> bool: ...

    def persist_plan(self, *, plan: TranscodePlan, now: datetime) -> None: ...


SUPPORTED_AV1AN_VERSION_FAMILY = "0.5.x"
ACCEPTED_CONTAINER_NAMES = {
    "mkv": ["matroska,webm"],
}
EXPECTED_AUDIO_CODEC_NAMES = {
    "libopus": "opus",
}
SVT_AV1_SDR_COLOR_ARGUMENTS = (
    ("--color-primaries", "1"),
    ("--transfer-characteristics", "1"),
    ("--matrix-coefficients", "1"),
    ("--color-range", "0"),
    ("--chroma-sample-position", "1"),
)


def parse_encoder_args(value: str) -> list[str]:
    return shlex.split(value)


def add_sdr_color_encoder_args(arguments: list[str]) -> list[str]:
    result = list(arguments)
    for option, value in SVT_AV1_SDR_COLOR_ARGUMENTS:
        if _encoder_option_present(result, option):
            continue
        result.extend([option, value])
    return result


def _encoder_option_present(arguments: list[str], option: str) -> bool:
    prefix = f"{option}="
    return any(argument == option or argument.startswith(prefix) for argument in arguments)


def build_profile_hash(
    profile: EncodingProfile,
    *,
    template_hash: str | None = None,
    script_hash: str | None = None,
) -> str:
    profile_payload = profile.model_dump(mode="json")
    if isinstance(profile_payload.get("vapoursynth"), dict):
        profile_payload["vapoursynth"].pop("script", None)
        profile_payload["vapoursynth"].pop("template", None)
    profile_payload["vapoursynth_template_hash"] = template_hash
    profile_payload["vapoursynth_script_hash"] = script_hash
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


def select_planning_inputs(
    store: PlanningStore,
    *,
    file_selectors: Sequence[Path],
    workspace_root: Path,
    resolve_path: Callable[[Path], Path],
) -> PlanningInputSelection:
    return store.select_inputs(
        file_selectors=file_selectors,
        workspace_root=workspace_root,
        resolve_path=resolve_path,
    )


def eligible_planning_inputs(store: PlanningStore) -> list[PlanningInputFile]:
    return store.eligible_inputs()


def load_stored_planning_context(
    store: PlanningStore,
    *,
    input_path: Path,
    resolved_profile: PlanningProfile,
) -> PlanningContext:
    return store.load_context(input_path=input_path, resolved_profile=resolved_profile)


def equivalent_plan_exists(
    store: PlanningStore,
    *,
    media_file_id: int | None,
    probe_hash: str,
    source_fs_fingerprint: str,
    profile_hash: str,
    execution_identity_hash: str,
) -> bool:
    return store.equivalent_current_plan_exists(
        media_file_id=media_file_id,
        probe_hash=probe_hash,
        source_fs_fingerprint=source_fs_fingerprint,
        profile_hash=profile_hash,
        execution_identity_hash=execution_identity_hash,
    )


def save_plan(store: PlanningStore, *, plan: TranscodePlan, now: datetime) -> None:
    store.persist_plan(plan=plan, now=now)


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
        "av1an_svt_av1_sdr_color_description": {
            "color_primaries": "bt709",
            "transfer_characteristics": "bt709",
            "matrix_coefficients": "bt709",
            "color_range": "studio",
            "chroma_sample_position": "left",
        },
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
    payload = f"{VALIDATION_POLICY_HASH_CONTRACT}\0".encode() + canonical_json(payload_data).encode(
        "utf-8"
    )
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def finalize_validation_policy(policy: ValidationPolicy) -> ValidationPolicy:
    return policy.model_copy(update={"policy_hash": build_validation_policy_hash(policy)})


def build_promotion_policy_hash(policy: PromotionPolicy) -> str:
    payload_data = policy.model_dump(mode="json")
    payload_data.pop("policy_hash", None)
    payload = f"{PROMOTION_POLICY_HASH_CONTRACT}\0".encode() + canonical_json(payload_data).encode(
        "utf-8"
    )
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def finalize_promotion_policy(policy: PromotionPolicy) -> PromotionPolicy:
    return policy.model_copy(update={"policy_hash": build_promotion_policy_hash(policy)})


def _absolute_stored_media_path(value: str, *, relative_path_root: Path | None = None) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    if relative_path_root is None:
        return path.resolve()
    return (relative_path_root / path).resolve()


def match_profile(
    profile: EncodingProfile,
    probe: NormalizedProbe,
) -> ProfileMatchResult:
    if not probe.video_streams:
        primary_video_codec = None
    else:
        primary_video = min(probe.video_streams, key=lambda stream: stream.index)
        primary_video_codec = primary_video.codec

    return evaluate_profile_match(
        ProfileMatchFacts(
            has_video_stream=bool(probe.video_streams),
            primary_video_codec=primary_video_codec,
        ),
        ProfileMatchPolicy(excluded_video_codecs=frozenset(profile.match.video_codec_not)),
    )


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
    try:
        return calculate_domain_target_dimensions(
            source_width=source_width,
            source_height=source_height,
            max_width=max_width,
        )
    except TargetDimensionError as exc:
        raise PlanningError(str(exc)) from exc


def select_audio(
    probe: NormalizedProbe,
    profile: EncodingProfile,
) -> AudioPlan | None:
    if not probe.audio_streams:
        return None

    language_rank = {
        language: index
        for index, language in enumerate(_normalized_languages(profile.audio.languages))
    }
    preferred = [
        stream
        for stream in probe.audio_streams
        if stream.language is not None and _normalize_language(stream.language) in language_rank
    ]
    eligible = preferred or list(probe.audio_streams)

    stream = min(
        eligible,
        key=lambda item: (
            language_rank.get(_normalize_language(item.language or ""), len(language_rank)),
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
    audio: AudioPlan | None,
    subtitles: SubtitlePlan,
    source_size_bytes: int,
) -> ValidationPolicy:
    if probe.duration_seconds is None or probe.duration_seconds <= 0:
        raise PlanningError("Source duration is missing or invalid.")
    accepted_containers = ACCEPTED_CONTAINER_NAMES.get(profile.container)
    if accepted_containers is None:
        raise PlanningError(f"Unsupported output container for validation: {profile.container}")
    expected_audio_codec = None
    if audio is not None:
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
        expected_audio_stream_count=1 if audio is not None else 0,
        expected_audio_codec=expected_audio_codec,
        expected_audio_channels=audio.target_channels if audio is not None else None,
        expected_audio_language=audio.source_language if audio is not None else None,
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
    runtime_identity: PlanningRuntimeIdentity | None = None,
    resolved_template: ResolvedVapourSynthTemplate | None = None,
    resolved_filter: ResolvedVapourSynthFilter | None = None,
    generator_version: int = GENERATOR_VERSION,
) -> TranscodePlan:
    match = match_profile(context.profile, context.normalized_probe)
    if not match.matched:
        raise ProfileNotApplicableError(match.reasons)

    media_file_id = context.media_file.id
    if media_file_id is None:
        raise PlanningError("Media file must be persisted before planning.")

    profile_template = _profile_template_path(context.profile)
    if context.profile.vapoursynth.mode == "custom_filter" and resolved_filter is None:
        resolved_filter = resolve_vapoursynth_filter(context.profile)
    if context.profile.vapoursynth.mode != "custom_filter" and resolved_filter is not None:
        raise PlanningError("Resolved filter was provided for a profile without a custom filter.")
    if context.profile.vapoursynth.mode == "custom_filter" and resolved_template is not None:
        raise PlanningError("custom_filter VapourSynth mode does not use a template.")
    if profile_template is None and resolved_template is not None:
        raise PlanningError("Resolved template was provided for a profile without a template.")
    if profile_template is not None and resolved_template is None:
        raise PlanningError("Profile requires a resolved VapourSynth template.")
    if (
        profile_template is not None
        and resolved_template is not None
        and profile_template != resolved_template.path
    ):
        raise PlanningError("Resolved template path does not match the selected profile.")

    mode = _vapoursynth_mode(context.profile, resolved_template, resolved_filter)
    template_hash = resolved_template.template_hash if resolved_template is not None else None
    script_hash = resolved_filter.script_hash if resolved_filter is not None else None
    vapoursynth_identity_hash = build_vapoursynth_identity_hash(
        generator_version=generator_version,
        mode=mode,
        output_format="YUV420P10",
        resize_filter="spline36",
        template_hash=template_hash,
        script_hash=script_hash,
        filter_entrypoint=resolved_filter.entrypoint if resolved_filter is not None else None,
        filter_api_version=resolved_filter.api_version if resolved_filter is not None else None,
    )
    profile_hash = build_profile_hash(
        context.profile,
        template_hash=template_hash,
        script_hash=script_hash,
    )
    execution_identity = build_execution_identity()
    promotion_policy = finalize_promotion_policy(PromotionPolicy(policy_hash=""))
    input_path = _absolute_stored_media_path(
        context.media_file.path,
        relative_path_root=context.relative_path_root,
    )
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
    runtime_identity = runtime_identity or default_planning_runtime_identity()
    encoder_args = add_sdr_color_encoder_args(parse_encoder_args(context.profile.av1an.video_args))

    plan = TranscodePlan(
        plan_hash="",
        input_path=input_path,
        output_path=paths.output_path,
        temp_dir=paths.work_dir,
        media_file_id=media_file_id,
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
            filter_path=(
                paths.artifacts.artifact_dir / "vpy" / "user_filter.py"
                if resolved_filter is not None
                else None
            ),
            filter_hash=script_hash,
            filter_entrypoint=resolved_filter.entrypoint if resolved_filter is not None else None,
            filter_api_version=resolved_filter.api_version if resolved_filter is not None else None,
            environment_id=runtime_identity.environment_id,
            environment_manifest_hash=runtime_identity.manifest_hash,
            avarch_image_digest=runtime_identity.avarch_image_digest,
            python_version=runtime_identity.python_version,
            python_abi=runtime_identity.python_abi,
            runtime_platform=runtime_identity.platform,
            vapoursynth_version=runtime_identity.vapoursynth_version,
            template_api_version=(
                context.profile.vapoursynth.api_version
                if context.profile.vapoursynth.mode == "custom_template"
                else None
            ),
            identity_hash=vapoursynth_identity_hash,
        ),
        av1an=Av1anCommandSpec(
            input_path=paths.artifacts.vapoursynth_script,
            video_output_path=paths.video_output_path,
            temp_dir=paths.av1an_temp_dir,
            working_directory=paths.work_dir,
            encoder=context.profile.av1an.encoder,
            encoder_args=encoder_args,
            workers=context.profile.av1an.workers,
        ),
        mux=FfmpegMuxSpec(
            video_input_path=paths.video_output_path,
            source_input_path=input_path,
            output_path=paths.output_path,
            audio_stream_index=audio.source_stream_index if audio is not None else None,
            subtitle_stream_indexes=[stream.source_stream_index for stream in subtitles.streams],
            audio_codec=audio.target_codec if audio is not None else None,
            audio_bitrate=audio.target_bitrate if audio is not None else None,
            audio_channels=audio.target_channels if audio is not None else None,
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


def default_planning_runtime_identity() -> PlanningRuntimeIdentity:
    return PlanningRuntimeIdentity(
        manifest_hash="unknown",
        avarch_image_digest="unknown",
        python_version="unknown",
        python_abi="unknown",
        platform="unknown",
        vapoursynth_version="unknown",
        environment_id="unknown",
    )


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


def _profile_template_path(profile: EncodingProfile) -> Path | None:
    if profile.vapoursynth.mode == "custom_template":
        return profile.vapoursynth.template
    return None


def _vapoursynth_mode(
    profile: EncodingProfile,
    resolved_template: ResolvedVapourSynthTemplate | None,
    resolved_filter: ResolvedVapourSynthFilter | None,
) -> VapourSynthMode:
    if resolved_filter is not None:
        return "custom_filter"
    if resolved_template is not None:
        return "custom_template"
    if profile.vapoursynth.mode == "custom_filter":
        return "custom_filter"
    return "generated"
