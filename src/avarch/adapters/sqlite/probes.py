from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlmodel import Session

from avarch.adapters.sqlite.models import MediaFile, ProbeResult
from avarch.models.probe import NormalizedProbe
from avarch.probe import build_probe_hash
from avarch.serialization import canonical_json


def store_probe_result(
    session: Session,
    *,
    media_file: MediaFile,
    raw_probe: Mapping[str, Any],
    normalized_probe: NormalizedProbe,
    created_at: datetime,
) -> ProbeResult:
    if media_file.id is None:
        raise ValueError("media_file must be persisted before storing probe results")

    probe_result = ProbeResult(
        media_file_id=media_file.id,
        ffprobe_json=canonical_json(raw_probe),
        normalized_json=canonical_json(normalized_probe),
        probe_hash=build_probe_hash(normalized_probe),
        source_fs_fingerprint=media_file.fs_fingerprint,
        created_at=created_at,
    )
    session.add(probe_result)
    session.flush()
    if probe_result.id is None:
        raise RuntimeError("probe result id was not assigned after flush")

    media_file.latest_probe_id = probe_result.id
    session.add(media_file)
    return probe_result


def get_canonical_probe_result(
    session: Session,
    media_file: MediaFile,
) -> ProbeResult | None:
    if media_file.id is None or media_file.latest_probe_id is None:
        return None

    probe_result = session.get(ProbeResult, media_file.latest_probe_id)
    if probe_result is None:
        return None
    if probe_result.media_file_id != media_file.id:
        return None
    if probe_result.source_fs_fingerprint != media_file.fs_fingerprint:
        return None
    return probe_result
