from __future__ import annotations

import hashlib
import os
import threading

from avarch.adapters.execution import (
    ProcessOutputRecord,
    build_av1an_command,
    run_managed_process,
)
from avarch.adapters.progress.av1an_tty import Av1anTtyProgressParser
from avarch.adapters.system.process_telemetry import LinuxProcessTelemetryObserver
from avarch.application.benchmark_samples import BenchmarkSample
from avarch.application.calibration_runner import CalibrationProgressSink
from avarch.domain.progress import ProgressSnapshot, ProgressSource
from avarch.models.execution import ProcessCancellationToken, ProcessResult
from avarch.models.plan import Av1anCommandSpec
from avarch.serialization import canonical_json


class ManagedCalibrationProcessRunner:
    def __init__(self, *, plan_hash: str) -> None:
        self._plan_hash = plan_hash

    def run(
        self,
        spec: Av1anCommandSpec,
        *,
        sample: BenchmarkSample,
        cancellation_token: ProcessCancellationToken | None,
        progress_sink: CalibrationProgressSink,
        timeout_seconds: float | None,
    ) -> ProcessResult:
        del sample
        command = build_av1an_command(spec, resume=False)
        collector = _CalibrationProgressCollector(progress_sink)
        bounded_cancellation = _BoundedCancellationToken(cancellation_token)
        timer = (
            threading.Timer(
                timeout_seconds,
                bounded_cancellation.request,
                kwargs={"reason": "calibration budget exhausted"},
            )
            if timeout_seconds is not None
            else None
        )
        if timer is not None:
            timer.start()
        try:
            result = run_managed_process(
                command,
                cwd=spec.working_directory,
                stdout_log=spec.working_directory / "stdout.log",
                stderr_log=spec.working_directory / "stderr.log",
                plan_hash=self._plan_hash,
                command_hash=_command_hash(command),
                stderr_callback=collector.callback,
                cancellation_token=bounded_cancellation,
                stderr_tty=os.name == "posix",
                process_observer=LinuxProcessTelemetryObserver() if os.name == "posix" else None,
            )
        finally:
            if timer is not None:
                timer.cancel()
        collector.flush()
        return result


class _BoundedCancellationToken(ProcessCancellationToken):
    def __init__(self, parent: ProcessCancellationToken | None) -> None:
        super().__init__()
        self._parent = parent

    @property
    def cancel_requested(self) -> bool:
        return super().cancel_requested or (
            self._parent is not None and self._parent.cancel_requested
        )

    @property
    def reason(self) -> str | None:
        if self._parent is not None and self._parent.cancel_requested:
            return self._parent.reason
        return super().reason


class _CalibrationProgressCollector:
    def __init__(self, sink: CalibrationProgressSink) -> None:
        self._sink = sink
        self._parser = Av1anTtyProgressParser()

    def callback(self, record: ProcessOutputRecord) -> None:
        if record.stream != "stderr":
            return
        for sample in self._parser.feed(record.data):
            self._sink.publish(_snapshot(sample))

    def flush(self) -> None:
        for sample in self._parser.flush():
            self._sink.publish(_snapshot(sample))


def _snapshot(sample: object) -> ProgressSnapshot:
    from avarch.adapters.progress.av1an_tty import Av1anTtyProgressSample

    if not isinstance(sample, Av1anTtyProgressSample):
        raise TypeError("unexpected Av1an progress sample")
    now = _utc_now()
    return ProgressSnapshot(
        phase=sample.phase,
        current=float(sample.current) if sample.current is not None else None,
        total=float(sample.total) if sample.total is not None else None,
        unit=sample.unit,
        rate_per_second=sample.rate_per_second,
        speed_ratio=sample.speed_ratio,
        source=ProgressSource.AV1AN_OUTPUT,
        message=sample.message,
        phase_started_at=now,
        observed_at=now,
        heartbeat_at=now,
        advanced_at=now,
        chunks_current=sample.chunks_current,
        chunks_total=sample.chunks_total,
        bitrate_kbps=sample.bitrate_kbps,
        estimated_output_bytes=sample.estimated_output_bytes,
    )


def _command_hash(command: list[str]) -> str:
    return hashlib.blake2b(canonical_json(command).encode("utf-8"), digest_size=32).hexdigest()


def _utc_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
