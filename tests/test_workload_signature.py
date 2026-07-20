from __future__ import annotations

from avarch.application.workload_signature import (
    WORKLOAD_SIGNATURE_SCHEMA_VERSION,
    build_workload_signature,
)
from tests.test_plan_models import sample_plan


def test_workload_signature_is_stable_across_operational_worker_changes() -> None:
    plan = sample_plan().model_copy(update={"semantic_hash": "semantic-hash"})
    changed_workers = plan.model_copy(
        update={"av1an": plan.av1an.model_copy(update={"workers": 2})}
    )

    first = build_workload_signature(plan)
    second = build_workload_signature(changed_workers)

    assert first.schema_version == WORKLOAD_SIGNATURE_SCHEMA_VERSION
    assert first.signature_hash == second.signature_hash
    assert first.source_codec_family == "hevc"
    assert first.source_width == 3840
    assert first.target_width == 1920
    assert first.duration_seconds == 600.0


def test_workload_signature_changes_with_profile_semantics() -> None:
    plan = sample_plan().model_copy(update={"semantic_hash": "semantic-hash"})
    changed_profile = plan.model_copy(update={"profile_hash": "different-profile"})

    assert build_workload_signature(plan).signature_hash != build_workload_signature(
        changed_profile
    ).signature_hash


def test_workload_signature_changes_with_video_context() -> None:
    plan = sample_plan().model_copy(update={"semantic_hash": "semantic-hash"})
    changed_video = plan.model_copy(
        update={"video": plan.video.model_copy(update={"source_width": 1920})}
    )

    assert build_workload_signature(plan).signature_hash != build_workload_signature(
        changed_video
    ).signature_hash


def test_workload_signature_payload_contains_hash_for_storage() -> None:
    signature = build_workload_signature(
        sample_plan().model_copy(update={"semantic_hash": "semantic-hash"})
    )

    payload = signature.to_payload()

    assert payload["signature_hash"] == signature.signature_hash
    assert payload["semantic_hash"] == "semantic-hash"
    assert payload["vapoursynth_identity_hash"] == "identity-hash"
