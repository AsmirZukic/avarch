from avarch.models.probe import NormalizedProbe, VideoStream
from avarch.probe import build_probe_hash, canonical_json


def test_canonical_json_sorts_object_keys() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_probe_hash_is_deterministic() -> None:
    probe = NormalizedProbe(container="matroska,webm")

    assert build_probe_hash(probe) == build_probe_hash(probe)


def test_probe_hash_ignores_raw_json_key_order() -> None:
    left = NormalizedProbe(container="matroska,webm", duration_seconds=1.0)
    right = NormalizedProbe(duration_seconds=1.0, container="matroska,webm")

    assert build_probe_hash(left) == build_probe_hash(right)


def test_probe_hash_changes_when_normalized_metadata_changes() -> None:
    left = NormalizedProbe(video_streams=[VideoStream(index=0, codec="hevc")])
    right = NormalizedProbe(video_streams=[VideoStream(index=0, codec="h264")])

    assert build_probe_hash(left) != build_probe_hash(right)


def test_probe_hash_does_not_depend_on_file_path() -> None:
    probe = NormalizedProbe(container="matroska,webm")

    assert build_probe_hash(probe) == build_probe_hash(probe)
