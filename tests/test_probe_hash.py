from avarch.adapters.probe import build_probe_hash
from avarch.models.probe import NormalizedProbe, VideoStream


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


def test_probe_hash_is_unchanged_after_serialization_refactor() -> None:
    probe = NormalizedProbe(
        container="matroska,webm",
        duration_seconds=1.0,
        video_streams=[VideoStream(index=0, codec="hevc")],
    )

    assert build_probe_hash(probe) == (
        "c6010b96740ba579b88bffeb3fc4a61576819c65a2000073aef57e814eb62efb"
    )
