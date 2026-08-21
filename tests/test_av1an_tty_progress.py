from pathlib import Path

from avarch.adapters.progress.av1an_tty import (
    Av1anTtyProgressParser,
    av1an_tty_progress_supported,
)
from avarch.domain.progress import ProgressPhase, ProgressUnit

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "av1an_progress"


def _parse_av1an_tty_progress(data: bytes):
    parser = Av1anTtyProgressParser()
    return [*parser.feed(data), *parser.flush()]


def test_parse_tty_fixture_extracts_numeric_progress_records() -> None:
    samples = _parse_av1an_tty_progress((FIXTURE_DIR / "encode_tty_raw.bin").read_bytes())
    first_numeric = next(sample for sample in samples if sample.current is not None)

    assert len(samples) >= 10
    assert first_numeric.phase == ProgressPhase.SCENE_DETECTION
    assert first_numeric.current == 0
    assert first_numeric.total == 120
    assert first_numeric.unit == ProgressUnit.FRAMES
    assert first_numeric.rate_per_second == 0.0
    assert first_numeric.speed_ratio is None


def test_parse_tty_fixture_extracts_scene_detection_result() -> None:
    samples = _parse_av1an_tty_progress((FIXTURE_DIR / "encode_tty_raw.bin").read_bytes())

    scene_samples = [sample for sample in samples if sample.phase == ProgressPhase.SCENE_DETECTION]

    assert any(sample.message == "scenes found: 1, chunks prepared: 1" for sample in scene_samples)


def test_parse_scene_detection_spinner_extracts_frames_fps_and_speed() -> None:
    samples = _parse_av1an_tty_progress(
        b"INFO encode_file: Input: 1920x1080 @ 24.000 fps\n"
        b"00:00:03 [========]  1,420 frames (384.0 fps)\r"
    )

    assert len(samples) == 1
    assert samples[0].phase == ProgressPhase.SCENE_DETECTION
    assert samples[0].current == 1420
    assert samples[0].total is None
    assert samples[0].rate_per_second == 384.0
    assert samples[0].speed_ratio == 16.0


def test_parse_scene_detection_result_includes_prepared_chunk_count() -> None:
    samples = _parse_av1an_tty_progress(
        b"INFO encode_file: scenecut: found 142 scene(s) "
        b"[with extra_splits (240 frames): 317 scene(s)]\n"
    )

    assert len(samples) == 1
    assert samples[0].phase == ProgressPhase.SCENE_DETECTION
    assert samples[0].message == "scenes found: 142, chunks prepared: 317"
    assert samples[0].chunks_total == 317


def test_parse_queue_line_marks_explicit_encoding_boundary() -> None:
    samples = _parse_av1an_tty_progress(b"Queue 317 Workers 4 Encoder svt-av1 Passes 1\n")

    assert len(samples) == 1
    assert samples[0].phase == ProgressPhase.ENCODING
    assert samples[0].message == "317 chunks queued, 4 workers"


def test_parse_tty_fixture_extracts_encoding_chunk_context() -> None:
    samples = _parse_av1an_tty_progress((FIXTURE_DIR / "encode_tty_raw.bin").read_bytes())

    encoding = [sample for sample in samples if sample.phase == ProgressPhase.ENCODING]

    assert encoding
    first_numeric = next(sample for sample in encoding if sample.current is not None)
    assert encoding[0].message == "1 chunks queued, 1 workers"
    assert first_numeric.message == "0/1 chunks"
    assert encoding[-1].current == 120
    assert encoding[-1].total == 120
    assert encoding[-1].rate_per_second is not None
    assert encoding[-1].rate_per_second > 0
    assert encoding[-1].speed_ratio is not None
    assert encoding[-1].speed_ratio > 1
    assert encoding[-1].message == "1/1 chunks, 38.0 Kbps, est. 23.19 KiB"
    assert encoding[-1].chunks_current == 1
    assert encoding[-1].chunks_total == 1
    assert encoding[-1].bitrate_kbps == 38
    assert encoding[-1].estimated_output_bytes == 23747


def test_parse_tty_encoding_line_includes_bitrate_context() -> None:
    samples = _parse_av1an_tty_progress(
        b"00:01:04 [9/317 Chunks] \xe2\x96\x90\xe2\x96\x8c   "
        b"7% 2345/31625 (36.46 fps, eta 13m, 1344.1 Kbps, "
        b"est. 211.35 MiB)\r"
    )

    assert len(samples) == 1
    assert samples[0].phase == ProgressPhase.ENCODING
    assert samples[0].current == 2345
    assert samples[0].total == 31625
    assert samples[0].rate_per_second == 36.46
    assert samples[0].message == "9/317 chunks, 1344.1 Kbps, est. 211.35 MiB"
    assert samples[0].chunks_current == 9
    assert samples[0].chunks_total == 317
    assert samples[0].bitrate_kbps == 1344
    assert samples[0].estimated_output_bytes == 221616538


def test_parse_tty_encoding_line_accepts_thousands_separators() -> None:
    samples = _parse_av1an_tty_progress(
        b"00:01:04 [1,009/2,317 Chunks] 2,345/31,625 "
        b"(36.46 fps, eta 13m, 1.5 Mbps, est. 1.25 GiB)\r"
    )

    assert len(samples) == 1
    assert samples[0].current == 2345
    assert samples[0].total == 31625
    assert samples[0].chunks_current == 1009
    assert samples[0].chunks_total == 2317
    assert samples[0].bitrate_kbps == 1500
    assert samples[0].estimated_output_bytes == 1342177280


def test_parse_non_tty_output_extracts_scene_detection_samples() -> None:
    stderr = (FIXTURE_DIR / "encode_non_tty_stderr.txt").read_bytes()

    samples = _parse_av1an_tty_progress(stderr)

    assert [sample.phase for sample in samples] == [
        ProgressPhase.SCENE_DETECTION,
        ProgressPhase.SCENE_DETECTION,
        ProgressPhase.ENCODING,
    ]
    assert samples[-2].message == "scenes found: 1, chunks prepared: 1"
    assert samples[-1].message == "1 chunks queued, 1 workers"


def test_parse_failure_output_extracts_scene_detection_before_failure() -> None:
    stderr = (FIXTURE_DIR / "encode_failure_stderr.txt").read_bytes()

    samples = _parse_av1an_tty_progress(stderr)

    assert any(sample.message == "scenes found: 1, chunks prepared: 1" for sample in samples)
    assert samples[-1].message == "1 chunks queued, 1 workers"


def test_parse_unsupported_record_returns_no_sample() -> None:
    assert _parse_av1an_tty_progress(b"progress: eighty percent maybe\n") == []


def test_parse_malformed_numeric_data_remains_harmless() -> None:
    assert _parse_av1an_tty_progress(b"00:00:00 [x/1 Chunks] nope/120 (nan fps)\r") == []


def test_av1an_tty_progress_supported_only_for_tested_version_family() -> None:
    assert av1an_tty_progress_supported("0.5.x") is True
    assert av1an_tty_progress_supported("0.6.x") is False
    assert av1an_tty_progress_supported("unknown") is False
    assert av1an_tty_progress_supported("0.5.x", enabled=False) is False


def test_incremental_parser_handles_one_record_split_across_reads() -> None:
    parser = Av1anTtyProgressParser()
    source = b"\x1b[1mencode_file\x1b[0m: Input: 160x90 @ 24.000 fps\n"
    record = b"\x1b[1m00:00:00\x1b[0m [0/1 Chunks] 48/120 (48 fps, eta 1s)\r"

    assert parser.feed(source) == []
    assert parser.feed(record[:17]) == []
    samples = parser.feed(record[17:])

    assert len(samples) == 1
    assert samples[0].phase == ProgressPhase.ENCODING
    assert samples[0].current == 48
    assert samples[0].speed_ratio == 2.0


def test_incremental_parser_handles_multiple_records_in_one_read() -> None:
    parser = Av1anTtyProgressParser()

    samples = parser.feed(
        b"00:00:00 [0/1 Chunks] 1/120 (10 fps, eta 1s)\r"
        b"00:00:00 [0/1 Chunks] 2/120 (11 fps, eta 1s)\n"
    )

    assert [sample.current for sample in samples] == [1, 2]


def test_incremental_parser_handles_common_line_endings() -> None:
    parser = Av1anTtyProgressParser()

    samples = parser.feed(
        b"00:00:00 1/120 (10 fps, eta 1s)\n"
        b"00:00:00 2/120 (11 fps, eta 1s)\r\n"
        b"00:00:00 3/120 (12 fps, eta 1s)\r"
    )

    assert [sample.current for sample in samples] == [1, 2, 3]


def test_incremental_parser_ignores_partial_final_record_until_flush() -> None:
    parser = Av1anTtyProgressParser()

    assert parser.feed(b"00:00:00 [0/1 Chunks] 12/120 (12 fps") == []
    samples = parser.flush()

    assert [sample.current for sample in samples] == [12]


def test_incremental_parser_ignores_malformed_utf8_and_unrelated_lines() -> None:
    parser = Av1anTtyProgressParser()

    samples = parser.feed(
        b"\xff\xfe not telemetry\r\x1b[2K00:00:00 [0/1 Chunks] 3/120 (13 fps, eta 1s)\r"
    )

    assert [sample.current for sample in samples] == [3]
