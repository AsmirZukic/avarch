from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from avarch.application.resources import ResourceConfidence, ResourceSnapshot, ResourceValue
from avarch.contracts import EXECUTION_ENVIRONMENT_HASH_CONTRACT
from avarch.serialization import canonical_json

EXECUTION_ENVIRONMENT_SIGNATURE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ExecutionEnvironmentSignature:
    schema_version: int
    signature_hash: str
    effective_cpu_count: int | None
    effective_cpu_quota: float | None
    effective_memory_bytes: int | None
    cpu_sources: tuple[str, ...]
    memory_sources: tuple[str, ...]
    tool_versions: dict[str, object]

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "signature_hash": self.signature_hash,
            "effective_cpu_count": self.effective_cpu_count,
            "effective_cpu_quota": self.effective_cpu_quota,
            "effective_memory_bytes": self.effective_memory_bytes,
            "cpu_sources": list(self.cpu_sources),
            "memory_sources": list(self.memory_sources),
            "tool_versions": self.tool_versions,
        }


def build_execution_environment_signature(
    *,
    snapshot: ResourceSnapshot,
    tool_versions: Mapping[str, object],
) -> ExecutionEnvironmentSignature:
    cpu_sources = tuple(_stable_sources(snapshot.cpu_values))
    memory_sources = tuple(_stable_sources(snapshot.memory_values))
    payload = {
        "schema_version": EXECUTION_ENVIRONMENT_SIGNATURE_SCHEMA_VERSION,
        "effective_cpu_count": snapshot.effective_cpu_count,
        "effective_cpu_quota": snapshot.effective_cpu_quota,
        "effective_memory_bytes": snapshot.effective_memory_bytes,
        "cpu_sources": list(cpu_sources),
        "memory_sources": list(memory_sources),
        "tool_versions": dict(sorted(tool_versions.items())),
    }
    signature_hash = build_execution_environment_signature_hash(payload)
    return ExecutionEnvironmentSignature(
        schema_version=EXECUTION_ENVIRONMENT_SIGNATURE_SCHEMA_VERSION,
        signature_hash=signature_hash,
        effective_cpu_count=snapshot.effective_cpu_count,
        effective_cpu_quota=snapshot.effective_cpu_quota,
        effective_memory_bytes=snapshot.effective_memory_bytes,
        cpu_sources=cpu_sources,
        memory_sources=memory_sources,
        tool_versions=dict(sorted(tool_versions.items())),
    )


def build_execution_environment_signature_payload(
    *,
    snapshot: ResourceSnapshot,
    tool_versions: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": EXECUTION_ENVIRONMENT_SIGNATURE_SCHEMA_VERSION,
        "effective_cpu_count": snapshot.effective_cpu_count,
        "effective_cpu_quota": snapshot.effective_cpu_quota,
        "effective_memory_bytes": snapshot.effective_memory_bytes,
        "cpu_sources": _stable_sources(snapshot.cpu_values),
        "memory_sources": _stable_sources(snapshot.memory_values),
        "tool_versions": dict(sorted(tool_versions.items())),
    }


def build_execution_environment_signature_hash(payload: Mapping[str, object]) -> str:
    data = f"{EXECUTION_ENVIRONMENT_HASH_CONTRACT}\0".encode() + canonical_json(
        dict(payload)
    ).encode("utf-8")
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def _stable_sources(values: Iterable[ResourceValue]) -> list[str]:
    sources = {
        value.source
        for value in values
        if value.confidence is ResourceConfidence.HIGH and value.value is not None
    }
    return sorted(sources)
