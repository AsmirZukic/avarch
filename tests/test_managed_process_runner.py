import os
import signal
import sys
import threading
import time
from pathlib import Path

from avarch.adapters.execution import (
    ProcessOutputCallback,
    ProcessOutputRecord,
    run_managed_process,
)
from avarch.models.execution import (
    ProcessCancellationToken,
    ProcessResult,
    ProcessTerminationReason,
)


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


def test_managed_process_gracefully_terminates_on_cancellation(tmp_path: Path) -> None:
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    token = ProcessCancellationToken()
    ready = threading.Event()

    def on_stdout(record: ProcessOutputRecord) -> None:
        if record.text == "ready\n":
            ready.set()

    child_code = """
import signal
import sys
import time

deadline = time.monotonic() + 1.5

def term(_signum, _frame):
    print("received-term", flush=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, term)
print("ready", flush=True)
while time.monotonic() < deadline:
    time.sleep(0.02)
sys.exit(7)
"""
    result_holder = _run_process_in_thread(
        [sys.executable, "-c", child_code],
        cwd=tmp_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        plan_hash="plan",
        command_hash="cancel",
        stdout_callback=on_stdout,
        cancellation_token=token,
        termination_grace_seconds=2.0,
    )

    assert ready.wait(timeout=1.0)
    token.request("test cancellation")
    result_holder.thread.join(timeout=3.0)

    assert not result_holder.thread.is_alive()
    result = result_holder.result
    assert result is not None
    assert result.termination_reason is ProcessTerminationReason.CANCELLED
    assert result.succeeded is False
    assert b"received-term\n" in stdout_log.read_bytes()
    assert b"interrupted=false" in stdout_log.read_bytes()


def test_managed_process_cancellation_terminates_descendant_process(
    tmp_path: Path,
) -> None:
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    token = ProcessCancellationToken()
    ready = threading.Event()
    child_pid_file = tmp_path / "child.pid"

    def on_stdout(record: ProcessOutputRecord) -> None:
        if record.text == "ready\n":
            ready.set()

    child_code = f"""
import signal
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path({str(child_pid_file)!r}).write_text(str(child.pid), encoding="utf-8")

def term(_signum, _frame):
    print("parent-term", flush=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, term)
print("ready", flush=True)
while True:
    time.sleep(0.05)
"""
    result_holder = _run_process_in_thread(
        [sys.executable, "-c", child_code],
        cwd=tmp_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        plan_hash="plan",
        command_hash="cancel-tree",
        stdout_callback=on_stdout,
        cancellation_token=token,
        termination_grace_seconds=1.0,
    )

    assert ready.wait(timeout=1.0)
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    token.request("cancel process tree")
    result_holder.thread.join(timeout=3.0)

    descendant_alive = _process_exists(child_pid)
    if descendant_alive:
        os.kill(child_pid, signal.SIGKILL)
        result_holder.thread.join(timeout=3.0)

    assert not result_holder.thread.is_alive()
    assert descendant_alive is False
    result = result_holder.result
    assert result is not None
    assert result.termination_reason is ProcessTerminationReason.CANCELLED


def test_managed_process_force_kills_process_group_after_grace_timeout(
    tmp_path: Path,
) -> None:
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    token = ProcessCancellationToken()
    ready = threading.Event()

    def on_stdout(record: ProcessOutputRecord) -> None:
        if record.text == "ready\n":
            ready.set()

    child_code = """
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ready", flush=True)
while True:
    time.sleep(0.05)
"""
    result_holder = _run_process_in_thread(
        [sys.executable, "-c", child_code],
        cwd=tmp_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        plan_hash="plan",
        command_hash="force-kill",
        stdout_callback=on_stdout,
        cancellation_token=token,
        termination_grace_seconds=0.1,
    )

    assert ready.wait(timeout=1.0)
    token.request("cancel uncooperative process")
    result_holder.thread.join(timeout=3.0)

    assert not result_holder.thread.is_alive()
    result = result_holder.result
    assert result is not None
    assert result.termination_reason is ProcessTerminationReason.FORCED_KILL
    assert result.succeeded is False


class _ThreadedProcessResult:
    def __init__(self, thread: threading.Thread) -> None:
        self.thread = thread
        self.result: ProcessResult | None = None


def _run_process_in_thread(
    command: list[str],
    *,
    cwd: Path,
    stdout_log: Path,
    stderr_log: Path,
    plan_hash: str,
    command_hash: str,
    stdout_callback: ProcessOutputCallback | None = None,
    cancellation_token: ProcessCancellationToken | None = None,
    termination_grace_seconds: float = 10.0,
) -> _ThreadedProcessResult:
    holder = _ThreadedProcessResult(thread=threading.Thread())

    def target() -> None:
        holder.result = run_managed_process(
            command,
            cwd=cwd,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            plan_hash=plan_hash,
            command_hash=command_hash,
            stdout_callback=stdout_callback,
            cancellation_token=cancellation_token,
            termination_grace_seconds=termination_grace_seconds,
        )

    holder.thread = threading.Thread(target=target)
    holder.thread.start()
    return holder


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        parts = proc_stat.read_text(encoding="utf-8").split()
        if len(parts) > 2 and parts[2] == "Z":
            return False
    return True


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
