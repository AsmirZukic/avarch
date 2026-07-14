from pathlib import Path

from avarch.adapters.progress.av1an_tty import parse_av1an_tty_progress
from avarch.domain.progress import ProgressPhase, ProgressUnit

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "av1an_progress"


def test_parse_tty_fixture_extracts_numeric_progress_records() -> None:
    samples = parse_av1an_tty_progress((FIXTURE_DIR / "encode_tty_raw.bin").read_bytes())

    assert len(samples) >= 10
    assert samples[0].phase == ProgressPhase.SCENE_DETECTION
    assert samples[0].current == 0
    assert samples[0].total == 120
    assert samples[0].unit == ProgressUnit.FRAMES
    assert samples[0].rate_per_second == 0.0
    assert samples[0].speed_ratio is None


def test_parse_tty_fixture_extracts_encoding_chunk_context() -> None:
    samples = parse_av1an_tty_progress((FIXTURE_DIR / "encode_tty_raw.bin").read_bytes())

    encoding = [sample for sample in samples if sample.phase == ProgressPhase.ENCODING]

    assert encoding
    assert encoding[0].message == "0/1 chunks"
    assert encoding[-1].current == 120
    assert encoding[-1].total == 120
    assert encoding[-1].rate_per_second is not None
    assert encoding[-1].rate_per_second > 0
    assert encoding[-1].message == "1/1 chunks"


def test_parse_non_tty_output_returns_no_samples() -> None:
    stderr = (FIXTURE_DIR / "encode_non_tty_stderr.txt").read_bytes()

    assert parse_av1an_tty_progress(stderr) == []


def test_parse_failure_output_returns_no_samples() -> None:
    stderr = (FIXTURE_DIR / "encode_failure_stderr.txt").read_bytes()

    assert parse_av1an_tty_progress(stderr) == []


def test_parse_unsupported_record_returns_no_sample() -> None:
    assert parse_av1an_tty_progress(b"progress: eighty percent maybe\n") == []
