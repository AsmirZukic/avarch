from __future__ import annotations

from avarch.application.environment_signature import (
    EXECUTION_ENVIRONMENT_SIGNATURE_SCHEMA_VERSION,
    build_execution_environment_signature,
)
from avarch.application.resources import ResourceConfidence, ResourceSnapshot, ResourceValue


def test_environment_signature_hash_is_deterministic_for_same_envelope() -> None:
    snapshot = _snapshot(cpu_count=4, memory_bytes=8 * 1024**3)

    left = build_execution_environment_signature(
        snapshot=snapshot,
        tool_versions={"svt_av1": "3.0.2", "av1an": "0.5.1"},
    )
    right = build_execution_environment_signature(
        snapshot=snapshot,
        tool_versions={"av1an": "0.5.1", "svt_av1": "3.0.2"},
    )

    assert left.schema_version == EXECUTION_ENVIRONMENT_SIGNATURE_SCHEMA_VERSION
    assert left.signature_hash == right.signature_hash
    assert left.cpu_sources == ("cgroup_v2", "cpuset")
    assert left.memory_sources == ("cgroup_v2",)


def test_environment_signature_changes_when_effective_limits_change() -> None:
    first = build_execution_environment_signature(
        snapshot=_snapshot(cpu_count=4, memory_bytes=8 * 1024**3),
        tool_versions={"av1an": "0.5.1"},
    )
    second = build_execution_environment_signature(
        snapshot=_snapshot(cpu_count=6, memory_bytes=8 * 1024**3),
        tool_versions={"av1an": "0.5.1"},
    )

    assert first.signature_hash != second.signature_hash


def test_environment_signature_excludes_low_confidence_sources_from_identity() -> None:
    first = build_execution_environment_signature(
        snapshot=_snapshot(cpu_count=4, memory_bytes=8 * 1024**3, host_cpu=16),
        tool_versions={"av1an": "0.5.1"},
    )
    second = build_execution_environment_signature(
        snapshot=_snapshot(cpu_count=4, memory_bytes=8 * 1024**3, host_cpu=32),
        tool_versions={"av1an": "0.5.1"},
    )

    assert first.signature_hash == second.signature_hash


def test_environment_signature_payload_contains_hash_for_storage() -> None:
    signature = build_execution_environment_signature(
        snapshot=_snapshot(cpu_count=4, memory_bytes=8 * 1024**3),
        tool_versions={"av1an": "0.5.1"},
    )

    payload = signature.to_payload()

    assert payload["signature_hash"] == signature.signature_hash
    assert payload["effective_cpu_count"] == 4
    assert payload["effective_memory_bytes"] == 8 * 1024**3


def _snapshot(
    *,
    cpu_count: int,
    memory_bytes: int,
    host_cpu: int = 16,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        effective_cpu_count=cpu_count,
        effective_cpu_quota=float(cpu_count),
        effective_memory_bytes=memory_bytes,
        cpu_values=(
            ResourceValue("cpuset", cpu_count, ResourceConfidence.HIGH),
            ResourceValue("cgroup_v2", float(cpu_count), ResourceConfidence.HIGH),
            ResourceValue("host_fallback", host_cpu, ResourceConfidence.LOW),
        ),
        memory_values=(
            ResourceValue("cgroup_v2", memory_bytes, ResourceConfidence.HIGH),
            ResourceValue("host_fallback", 64 * 1024**3, ResourceConfidence.LOW),
        ),
    )
