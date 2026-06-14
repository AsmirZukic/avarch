from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from avarch.config import AppConfig, EncodingProfile
from avarch.models.db import MediaFile, MediaFileStatus, ProbeResult
from avarch.models.probe import NormalizedProbe
from avarch.probe import ProbeError, parse_normalized_probe_json
from avarch.serialization import canonical_json


class PlanningError(RuntimeError):
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


def parse_encoder_args(value: str) -> list[str]:
    return shlex.split(value)


def build_profile_hash(profile: EncodingProfile) -> str:
    payload = b"profile-v1\0" + canonical_json(profile).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


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
