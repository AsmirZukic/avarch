from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "av1an_progress"


def test_non_tty_av1an_capture_has_phase_output_without_progress_bar() -> None:
    assert (FIXTURE_DIR / "encode_non_tty_stdout.txt").read_bytes() == b""

    stderr = (FIXTURE_DIR / "encode_non_tty_stderr.txt").read_text(encoding="utf-8")

    assert "Scene detection" in stderr
    assert "Queue 1 Workers 1 Encoder svt-av1 Passes 1" in stderr
    assert "0/120" not in stderr


def test_tty_av1an_capture_preserves_control_bytes_and_numeric_progress() -> None:
    raw = (FIXTURE_DIR / "encode_tty_raw.bin").read_bytes()

    assert b"\x1b[" in raw
    assert b"\r" in raw
    assert b"0/120" in raw
    assert b"120/120" in raw
    assert b"fps" in raw
    assert b"eta" in raw


def test_failure_capture_is_representative() -> None:
    failure = (FIXTURE_DIR / "encode_failure_stderr.txt").read_text(encoding="utf-8")
    failure_exit = (FIXTURE_DIR / "encode_failure_exit.txt").read_text(encoding="utf-8")

    assert failure_exit == "failure_code=1\n"
    assert "encoder failed" in failure
    assert "Unprocessed tokens: --definitely-not-an-svt-option" in failure
