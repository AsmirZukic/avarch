from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from avarch.contracts import WORKLOAD_SIGNATURE_HASH_CONTRACT
from avarch.models.plan import TranscodePlan
from avarch.serialization import canonical_json

WORKLOAD_SIGNATURE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class WorkloadSignature:
    schema_version: int
    signature_hash: str
    source_codec_family: str
    source_width: int
    source_height: int
    target_width: int
    target_height: int
    source_bit_depth: int | None
    source_hdr_metadata_present: bool
    hdr_to_sdr: bool
    duration_seconds: float
    profile_hash: str
    semantic_hash: str | None
    vapoursynth_identity_hash: str

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "signature_hash": self.signature_hash,
            "source_codec_family": self.source_codec_family,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "target_width": self.target_width,
            "target_height": self.target_height,
            "source_bit_depth": self.source_bit_depth,
            "source_hdr_metadata_present": self.source_hdr_metadata_present,
            "hdr_to_sdr": self.hdr_to_sdr,
            "duration_seconds": self.duration_seconds,
            "profile_hash": self.profile_hash,
            "semantic_hash": self.semantic_hash,
            "vapoursynth_identity_hash": self.vapoursynth_identity_hash,
        }


def build_workload_signature(plan: TranscodePlan) -> WorkloadSignature:
    source_codec_family = _codec_family(plan.video.source_codec)
    duration_seconds = round(plan.validation.source_duration_seconds, 3)
    payload = {
        "schema_version": WORKLOAD_SIGNATURE_SCHEMA_VERSION,
        "source_codec_family": source_codec_family,
        "source_width": plan.video.source_width,
        "source_height": plan.video.source_height,
        "target_width": plan.video.target_width,
        "target_height": plan.video.target_height,
        "source_bit_depth": plan.video.source_bit_depth,
        "source_hdr_metadata_present": plan.video.source_hdr_metadata_present,
        "hdr_to_sdr": plan.video.hdr_to_sdr,
        "duration_seconds": duration_seconds,
        "profile_hash": plan.profile_hash,
        "semantic_hash": plan.semantic_hash,
        "vapoursynth_identity_hash": plan.vapoursynth.identity_hash,
    }
    signature_hash = build_workload_signature_hash(payload)
    return WorkloadSignature(
        schema_version=WORKLOAD_SIGNATURE_SCHEMA_VERSION,
        signature_hash=signature_hash,
        source_codec_family=source_codec_family,
        source_width=plan.video.source_width,
        source_height=plan.video.source_height,
        target_width=plan.video.target_width,
        target_height=plan.video.target_height,
        source_bit_depth=plan.video.source_bit_depth,
        source_hdr_metadata_present=plan.video.source_hdr_metadata_present,
        hdr_to_sdr=plan.video.hdr_to_sdr,
        duration_seconds=duration_seconds,
        profile_hash=plan.profile_hash,
        semantic_hash=plan.semantic_hash,
        vapoursynth_identity_hash=plan.vapoursynth.identity_hash,
    )


def build_workload_signature_payload(plan: TranscodePlan) -> dict[str, object]:
    return {
        "schema_version": WORKLOAD_SIGNATURE_SCHEMA_VERSION,
        "source_codec_family": _codec_family(plan.video.source_codec),
        "source_width": plan.video.source_width,
        "source_height": plan.video.source_height,
        "target_width": plan.video.target_width,
        "target_height": plan.video.target_height,
        "source_bit_depth": plan.video.source_bit_depth,
        "source_hdr_metadata_present": plan.video.source_hdr_metadata_present,
        "hdr_to_sdr": plan.video.hdr_to_sdr,
        "duration_seconds": round(plan.validation.source_duration_seconds, 3),
        "profile_hash": plan.profile_hash,
        "semantic_hash": plan.semantic_hash,
        "vapoursynth_identity_hash": plan.vapoursynth.identity_hash,
    }


def build_workload_signature_hash(payload: Mapping[str, object]) -> str:
    data = f"{WORKLOAD_SIGNATURE_HASH_CONTRACT}\0".encode() + canonical_json(
        dict(payload)
    ).encode("utf-8")
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def _codec_family(codec: str) -> str:
    normalized = codec.strip().lower()
    if normalized in {"h264", "avc", "mpeg4 part 10"}:
        return "h264"
    if normalized in {"h265", "hevc"}:
        return "hevc"
    if normalized in {"av1"}:
        return "av1"
    if normalized in {"vp9"}:
        return "vp9"
    return normalized or "unknown"
