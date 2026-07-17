from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from rich.console import RenderableType
from rich.text import Text

from avarch.application.job_views import JobAttemptView, JobDetails
from avarch.application.scheduler_snapshot import (
    ActiveJobSummary,
    CapacitySummary,
    PipelineSummary,
    SchedulerRuntimeState,
    SchedulerRuntimeSummary,
    SchedulerSnapshot,
    WorkspaceSummary,
)
from avarch.application.scheduler_watch import SchedulerWatchLoop
from avarch.application.scheduler_watch_controller import WatchLogPaths
from avarch.application.scheduler_watch_keys import (
    KEY_CANCEL,
    KEY_CTRL_C,
    KEY_DETACH,
    KEY_DETAILS,
    KEY_LOGS,
    PosixKeySource,
    UnsupportedKeySource,
)
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus


def test_q_detaches_scheduler_watch() -> None:
    controller = _Controller()
    sink = _FakeSink()

    asyncio.run(_run_with_key("q", controller=controller, sink=sink))

    assert controller.calls == ["detach"]
    assert sink.texts == ["running"]


def test_ctrl_c_detaches_scheduler_watch() -> None:
    controller = _Controller()

    asyncio.run(_run_with_key(KEY_CTRL_C, controller=controller))

    assert controller.calls == ["detach"]


def test_ctrl_c_stops_scheduler_from_owner_dashboard() -> None:
    controller = _Controller()

    asyncio.run(
        _run_with_key_source(
            _KeySource([KEY_CTRL_C]),
            controller=controller,
            sink=_FakeSink(),
            ctrl_c_stops_scheduler=True,
        )
    )

    assert controller.calls == ["stop:owner dashboard"]


def test_d_detaches_scheduler_watch() -> None:
    controller = _Controller()

    asyncio.run(_run_with_key(KEY_DETACH, controller=controller))

    assert controller.calls == ["detach"]


def test_posix_key_source_reads_single_byte_from_pty() -> None:
    master_fd, slave_fd = os.openpty()
    try:
        with (
            os.fdopen(slave_fd, "r", encoding="utf-8", buffering=1) as slave,
            PosixKeySource(slave) as key_source,
        ):
            os.write(master_fd, b"d")
            assert key_source.poll_key() == KEY_DETACH
    finally:
        os.close(master_fd)


def test_posix_key_source_keeps_pty_output_blocking() -> None:
    master_fd, slave_fd = os.openpty()
    try:
        with os.fdopen(slave_fd, "r", encoding="utf-8", buffering=1) as slave:
            assert os.get_blocking(slave.fileno()) is True
            with PosixKeySource(slave):
                assert os.get_blocking(slave.fileno()) is True
    finally:
        os.close(master_fd)


def test_posix_key_source_reads_ctrl_c_instead_of_raising_signal() -> None:
    master_fd, slave_fd = os.openpty()
    try:
        with (
            os.fdopen(slave_fd, "r", encoding="utf-8", buffering=1) as slave,
            PosixKeySource(slave) as key_source,
        ):
            os.write(master_fd, b"\x03")
            assert key_source.poll_key() == KEY_CTRL_C
    finally:
        os.close(master_fd)


def test_p_pauses_when_scheduler_is_running() -> None:
    controller = _Controller()

    asyncio.run(_run_with_key("p", controller=controller, state=SchedulerRuntimeState.RUNNING))

    assert controller.calls == ["pause:watch"]


def test_p_resumes_when_scheduler_is_paused() -> None:
    controller = _Controller()

    asyncio.run(_run_with_key("p", controller=controller, state=SchedulerRuntimeState.PAUSED))

    assert controller.calls == ["resume"]


def test_unknown_key_takes_no_action() -> None:
    controller = _Controller()

    asyncio.run(_run_with_key("x", controller=controller))

    assert controller.calls == []


def test_enter_renders_selected_job_details() -> None:
    controller = _Controller(details=_job_details(42))
    sink = _FakeSink()

    asyncio.run(
        _run_with_key(
            KEY_DETAILS,
            controller=controller,
            sink=sink,
            active_jobs=(_active_job(42),),
            renderer=lambda snapshot, _width: Text(
                str(snapshot.watch_details.job_id if snapshot.watch_details else "none")
            ),
        )
    )

    assert sink.texts == ["42"]


def test_l_renders_selected_job_log_tail(tmp_path: Path) -> None:
    log_path = tmp_path / "stderr.log"
    log_path.write_text("one\ntwo\n", encoding="utf-8")
    controller = _Controller(stderr_log=str(log_path))
    sink = _FakeSink()

    asyncio.run(
        _run_with_key(
            KEY_LOGS,
            controller=controller,
            sink=sink,
            active_jobs=(_active_job(42),),
            renderer=lambda snapshot, _width: Text(
                "|".join(snapshot.watch_log_tail.lines) if snapshot.watch_log_tail else "none"
            ),
        )
    )

    assert sink.texts == ["one|two"]


def test_second_c_confirms_selected_job_cancel() -> None:
    controller = _Controller()

    asyncio.run(
        _run_with_key_source(
            _KeySource([KEY_CANCEL, KEY_CANCEL]),
            controller=controller,
            sink=_FakeSink(),
            snapshots=(_snapshot(active_jobs=(_active_job(42),)),) * 2,
            stop_after_iterations=2,
        )
    )

    assert controller.calls == ["cancel:42:watch"]


def test_unsupported_key_source_disables_shortcuts_cleanly() -> None:
    controller = _Controller()
    sink = _FakeSink()

    asyncio.run(_run_with_key_source(UnsupportedKeySource(), controller=controller, sink=sink))

    assert controller.calls == []
    assert sink.texts == ["running"]


async def _run_with_key(
    key: str,
    *,
    controller: _Controller,
    sink: _FakeSink | None = None,
    state: SchedulerRuntimeState = SchedulerRuntimeState.RUNNING,
    active_jobs: tuple[ActiveJobSummary, ...] = (),
    renderer: object | None = None,
) -> None:
    await _run_with_key_source(
        _KeySource([key]),
        controller=controller,
        sink=sink or _FakeSink(),
        snapshots=(_snapshot(state, active_jobs=active_jobs),),
        renderer=renderer,
    )


async def _run_with_key_source(
    key_source: object,
    *,
    controller: _Controller,
    sink: _FakeSink,
    snapshots: Sequence[SchedulerSnapshot] | None = None,
    renderer: object | None = None,
    stop_after_iterations: int = 1,
    ctrl_c_stops_scheduler: bool = False,
) -> None:
    renderer = renderer or (lambda snapshot, _width: Text(snapshot.scheduler.state.value))
    loop = SchedulerWatchLoop(
        snapshot_query=_Query(snapshots or (_snapshot(),)),
        renderer=renderer,  # type: ignore[arg-type]
        sink=sink,
        interval_seconds=1,
        terminal_width=lambda: 100,
        sleeper=lambda _interval: asyncio.sleep(0),
        stop_after_iterations=stop_after_iterations,
        key_source=key_source,  # type: ignore[arg-type]
        watch_controller=controller,
        ctrl_c_stops_scheduler=ctrl_c_stops_scheduler,
    )
    await loop.run()


class _KeySource:
    supported = True

    def __init__(self, keys: Sequence[str]) -> None:
        self._keys = list(keys)

    def poll_key(self) -> str | None:
        if not self._keys:
            return None
        return self._keys.pop(0)


class _Controller:
    def __init__(
        self,
        *,
        details: JobDetails | None = None,
        stderr_log: str | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._details = details
        self._stderr_log = stderr_log

    def pause(self, *, reason: str | None = None) -> object:
        self.calls.append(f"pause:{reason}")
        return object()

    def resume(self) -> object:
        self.calls.append("resume")
        return object()

    def cancel(self, *, job_id: int, reason: str | None = None) -> object:
        self.calls.append(f"cancel:{job_id}:{reason}")
        return object()

    def details(self, *, job_id: int) -> JobDetails | None:
        del job_id
        return self._details

    def log_paths(self, *, job_id: int, attempt_number: int | None = None) -> WatchLogPaths:
        del job_id, attempt_number
        return WatchLogPaths(stdout_log=None, stderr_log=self._stderr_log)

    def detach(self) -> object:
        self.calls.append("detach")
        return object()

    def stop(self, *, reason: str | None = None) -> object:
        self.calls.append(f"stop:{reason}")
        return object()


class _FakeSink:
    def __init__(self) -> None:
        self.renderables: list[RenderableType] = []
        self.texts: list[str] = []

    def render(self, renderable: RenderableType) -> None:
        self.renderables.append(renderable)
        self.texts.append(str(renderable))


class _Query:
    def __init__(self, snapshots: Sequence[SchedulerSnapshot]) -> None:
        self._snapshots = list(snapshots)

    def snapshot(self) -> SchedulerSnapshot:
        return self._snapshots.pop(0)


def _snapshot(
    state: SchedulerRuntimeState = SchedulerRuntimeState.RUNNING,
    *,
    active_jobs: tuple[ActiveJobSummary, ...] = (),
) -> SchedulerSnapshot:
    return SchedulerSnapshot(
        captured_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        workspace=WorkspaceSummary(root_path="/workspace"),
        scheduler=SchedulerRuntimeSummary(state=state),
        pipeline=PipelineSummary(queued=0, active=len(active_jobs), completed=0, failed=0),
        active_jobs=active_jobs,
        capacity=CapacitySummary(cheap_workers=0, av1an_jobs=0, file_ops=0),
        upcoming_jobs=(),
        recent_events=(),
        alerts=(),
        resources=None,
        forecast=None,
    )


def _active_job(job_id: int) -> ActiveJobSummary:
    return ActiveJobSummary(
        job_id=job_id,
        source_path=f"/media/{job_id}.mkv",
        status=JobStatus.ENCODING,
        stage=JobStage.ENCODE,
        priority=0,
        workflow_steps=(),
    )


def _job_details(job_id: int) -> JobDetails:
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    return JobDetails(
        id=job_id,
        source_path="/media/movie.mkv",
        profile_name="default",
        profile_hash="profile",
        queue_key="queue",
        priority=0,
        status=JobStatus.ENCODING,
        stage=JobStage.ENCODE,
        claimed_by="runner",
        attempts=1,
        created_at=now,
        started_at=now,
        finished_at=None,
        cancel_requested_at=None,
        hold_requested_at=None,
        probe_hash="probe",
        plan_hash="plan",
        plan_path="/plans/plan.json",
        output_path="/work/movie.av1.mkv",
        latest_validation_id=None,
        latest_promotion_id=None,
        last_error_type=None,
        last_error_message=None,
        attempt_history=[
            JobAttemptView(
                attempt_number=1,
                stage=JobStage.ENCODE,
                status=AttemptStatus.RUNNING,
                runner_id="runner",
                stdout_log="/logs/stdout.log",
                stderr_log="/logs/stderr.log",
            )
        ],
        events=[],
    )
