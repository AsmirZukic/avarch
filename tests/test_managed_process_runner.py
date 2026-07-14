import sys
import threading
import time
from pathlib import Path

from avarch.adapters.execution import ProcessOutputRecord, run_managed_process
from avarch.models.execution import ProcessCancellationToken, ProcessTerminationReason


def test_managed_process_streams_stdout_before_child_exits(tmp_path: Path) -> None:
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    first_record_seen = threading.Event()
    callback_times: list[float] = []

    def on_stdout(record: ProcessOutputRecord) -> None:
        assert record.stream == "stdout"
        callback_times.append(time.monotonic())
        first_record_seen.set()

    started = time.monotonic()
    result = run_managed_process(
        [
            sys.executable,
            "-c",
            "import sys, time; sys.stdout.write('first\\n'); sys.stdout.flush(); "
            "time.sleep(0.4); sys.stdout.write('second\\n'); sys.stdout.flush()",
        ],
        cwd=tmp_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        plan_hash="plan",
        command_hash="command",
        stdout_callback=on_stdout,
    )
    finished = time.monotonic()

    assert first_record_seen.is_set()
    assert callback_times[0] - started < 0.3
    assert finished - callback_times[0] >= 0.3
    assert result.return_code == 0
    assert result.termination_reason is ProcessTerminationReason.EXITED


def test_managed_process_tees_stdout_to_log_and_callback_in_order(tmp_path: Path) -> None:
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    records: list[bytes] = []

    result = run_managed_process(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'alpha\\nbeta\\npartial'); sys.stdout.flush()",
        ],
        cwd=tmp_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        plan_hash="plan",
        command_hash="command",
        stdout_callback=lambda record: records.append(record.data),
    )

    assert result.succeeded is True
    assert records == [b"alpha\n", b"beta\n", b"partial"]
    log_bytes = stdout_log.read_bytes()
    assert b"alpha\nbeta\npartial" in log_bytes


def test_managed_process_handles_empty_and_non_utf8_stdout(tmp_path: Path) -> None:
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    records: list[ProcessOutputRecord] = []

    empty = run_managed_process(
        [sys.executable, "-c", ""],
        cwd=tmp_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        plan_hash="plan",
        command_hash="empty",
        stdout_callback=records.append,
    )
    assert empty.succeeded is True
    assert records == []

    non_utf8 = run_managed_process(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'bad: \\xff')"],
        cwd=tmp_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        plan_hash="plan",
        command_hash="non-utf8",
        stdout_callback=records.append,
    )

    assert non_utf8.succeeded is True
    assert records[-1].data == b"bad: \xff"
    assert records[-1].text == "bad: \ufffd"


def test_managed_process_accepts_unset_cancellation_token(tmp_path: Path) -> None:
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"

    result = run_managed_process(
        [sys.executable, "-c", "print('ok')"],
        cwd=tmp_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        plan_hash="plan",
        command_hash="token",
        cancellation_token=ProcessCancellationToken(),
    )

    assert result.succeeded is True


def test_managed_process_streams_stdout_and_stderr_to_separate_logs(tmp_path: Path) -> None:
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    stdout_records: list[bytes] = []
    stderr_records: list[bytes] = []

    result = run_managed_process(
        [
            sys.executable,
            "-c",
            "import sys; "
            "sys.stdout.write('out-1\\n'); sys.stdout.flush(); "
            "sys.stderr.write('err-1\\n'); sys.stderr.flush(); "
            "sys.stdout.write('out-2'); sys.stdout.flush(); "
            "sys.stderr.write('err-2'); sys.stderr.flush()",
        ],
        cwd=tmp_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        plan_hash="plan",
        command_hash="interleaved",
        stdout_callback=lambda record: stdout_records.append(record.data),
        stderr_callback=lambda record: stderr_records.append(record.data),
    )

    assert result.succeeded is True
    assert stdout_records == [b"out-1\n", b"out-2"]
    assert stderr_records == [b"err-1\n", b"err-2"]
    assert b"out-1\nout-2" in stdout_log.read_bytes()
    assert b"err-1\nerr-2" in stderr_log.read_bytes()


def test_managed_process_does_not_deadlock_on_large_stdout_and_stderr(
    tmp_path: Path,
) -> None:
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"

    result = run_managed_process(
        [
            sys.executable,
            "-c",
            "import sys; "
            "sys.stdout.buffer.write(b'o' * 200000); sys.stdout.flush(); "
            "sys.stderr.buffer.write(b'e' * 200000); sys.stderr.flush()",
        ],
        cwd=tmp_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        plan_hash="plan",
        command_hash="large",
    )

    assert result.succeeded is True
    assert b"o" * 200000 in stdout_log.read_bytes()
    assert b"e" * 200000 in stderr_log.read_bytes()


def test_managed_process_preserves_stderr_partial_and_carriage_records(
    tmp_path: Path,
) -> None:
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    stderr_records: list[bytes] = []

    result = run_managed_process(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.buffer.write(b'progress\\rpartial'); sys.stderr.flush()",
        ],
        cwd=tmp_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        plan_hash="plan",
        command_hash="stderr-partial",
        stderr_callback=lambda record: stderr_records.append(record.data),
    )

    assert result.succeeded is True
    assert stderr_records == [b"progress\r", b"partial"]
    assert b"progress\rpartial" in stderr_log.read_bytes()
