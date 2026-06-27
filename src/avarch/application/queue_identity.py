from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from avarch.application.planning import build_execution_identity, build_profile_hash
from avarch.application.vapoursynth_identity import (
    GENERATOR_VERSION,
    build_vapoursynth_identity_hash,
    resolve_vapoursynth_filter,
    resolve_vapoursynth_template,
)
from avarch.config import AppConfig
from avarch.contracts import QUEUE_CONTRACT
from avarch.profiles.registry import ProfileRegistry, ResolvedProfile, UnknownProfileError
from avarch.serialization import canonical_json


class QueueIdentityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlanningIdentity:
    profile_hash: str
    vapoursynth_identity_hash: str
    execution_identity_hash: str


def build_queue_key(
    *,
    media_path: Path,
    source_fs_fingerprint: str,
    profile_name: str,
    profile_hash: str,
    probe_hash: str | None,
    vapoursynth_identity_hash: str,
    execution_identity_hash: str,
) -> str:
    payload_json = canonical_json(
        {
            "media_path": str(media_path.resolve()),
            "source_fs_fingerprint": source_fs_fingerprint,
            "profile_name": profile_name,
            "profile_hash": profile_hash,
            "probe_hash": probe_hash,
            "vapoursynth_identity_hash": vapoursynth_identity_hash,
            "execution_identity_hash": execution_identity_hash,
            "queue_contract": QUEUE_CONTRACT,
        }
    )
    payload = f"{QUEUE_CONTRACT}\0".encode() + payload_json.encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def planning_identity(resolved_profile: ResolvedProfile) -> PlanningIdentity:
    profile = resolved_profile.profile
    resolved_template = resolve_vapoursynth_template(profile)
    resolved_filter = resolve_vapoursynth_filter(profile)
    mode = (
        "custom_filter"
        if resolved_filter is not None
        else "custom_template"
        if resolved_template is not None
        else "generated"
    )
    template_hash = resolved_template.template_hash if resolved_template is not None else None
    script_hash = resolved_filter.script_hash if resolved_filter is not None else None
    vapoursynth_identity_hash = build_vapoursynth_identity_hash(
        generator_version=GENERATOR_VERSION,
        mode=mode,
        output_format="YUV420P10",
        resize_filter="spline36",
        template_hash=template_hash,
        script_hash=script_hash,
        filter_entrypoint=resolved_filter.entrypoint if resolved_filter is not None else None,
        filter_api_version=resolved_filter.api_version if resolved_filter is not None else None,
    )
    execution_identity = build_execution_identity()
    return PlanningIdentity(
        profile_hash=build_profile_hash(
            profile,
            template_hash=template_hash,
            script_hash=script_hash,
        ),
        vapoursynth_identity_hash=vapoursynth_identity_hash,
        execution_identity_hash=execution_identity.identity_hash,
    )


def resolve_profile(config: AppConfig, profile_name: str) -> ResolvedProfile:
    try:
        return ProfileRegistry.from_config(config).get(profile_name)
    except UnknownProfileError as exc:
        raise QueueIdentityError(f"Unknown profile: {profile_name}") from exc
    except Exception as exc:
        raise QueueIdentityError(str(exc)) from exc
