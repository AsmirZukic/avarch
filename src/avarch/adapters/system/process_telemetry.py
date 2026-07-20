from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from avarch.models.execution import ProcessResourceSummary


@dataclass(frozen=True, slots=True)
class ProcessTreeResourceSample:
    pids: tuple[int, ...]
    rss_bytes: int | None
    swap_bytes: int | None
    attribution_available: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CgroupPressureSample:
    memory_oom_events: int | None = None
    memory_oom_kill_events: int | None = None
    swap_current_bytes: int | None = None
    cpu_throttled_events: int | None = None
    cpu_throttled_usec: int | None = None


class LinuxProcessTreeSampler:
    def __init__(self, *, proc_root: Path = Path("/proc")) -> None:
        self._proc_root = proc_root

    def sample(self, *, root_pid: int) -> ProcessTreeResourceSample:
        parent_map = _read_parent_map(self._proc_root)
        if parent_map is None:
            return ProcessTreeResourceSample(
                pids=(),
                rss_bytes=None,
                swap_bytes=None,
                attribution_available=False,
                reason="process_tree_unavailable",
            )
        pids = _descendant_pids(root_pid, parent_map)
        rss_total = 0
        swap_total = 0
        readable = False
        for pid in pids:
            usage = _read_process_usage(self._proc_root / str(pid) / "status")
            if usage is None:
                continue
            readable = True
            rss_total += usage[0]
            swap_total += usage[1]
        if not readable:
            return ProcessTreeResourceSample(
                pids=pids,
                rss_bytes=None,
                swap_bytes=None,
                attribution_available=False,
                reason="process_usage_unavailable",
            )
        return ProcessTreeResourceSample(
            pids=pids,
            rss_bytes=rss_total,
            swap_bytes=swap_total,
            attribution_available=True,
        )


class LinuxProcessTelemetryObserver:
    def __init__(
        self,
        *,
        proc_root: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
    ) -> None:
        self._tree_sampler = LinuxProcessTreeSampler(proc_root=proc_root)
        self._cgroup_root = cgroup_root
        self._pressure_before: CgroupPressureSample | None = None
        self._samples: list[ProcessTreeResourceSample] = []

    def before_start(self) -> None:
        self._pressure_before = read_cgroup_pressure(self._cgroup_root)

    def sample(self, root_pid: int) -> None:
        self._samples.append(self._tree_sampler.sample(root_pid=root_pid))

    def after_exit(self, root_pid: int | None) -> ProcessResourceSummary:
        if root_pid is not None:
            self.sample(root_pid)
        pressure_after = read_cgroup_pressure(self._cgroup_root)
        return summarize_process_resources(
            samples=tuple(self._samples),
            pressure_before=self._pressure_before,
            pressure_after=pressure_after,
        )


def read_cgroup_pressure(cgroup_root: Path) -> CgroupPressureSample:
    memory_events = _read_keyed_int_file(cgroup_root / "memory.events")
    cpu_stat = _read_keyed_int_file(cgroup_root / "cpu.stat")
    return CgroupPressureSample(
        memory_oom_events=memory_events.get("oom") if memory_events is not None else None,
        memory_oom_kill_events=(
            memory_events.get("oom_kill") if memory_events is not None else None
        ),
        swap_current_bytes=_read_int(cgroup_root / "memory.swap.current"),
        cpu_throttled_events=cpu_stat.get("nr_throttled") if cpu_stat is not None else None,
        cpu_throttled_usec=cpu_stat.get("throttled_usec") if cpu_stat is not None else None,
    )


def summarize_process_resources(
    *,
    samples: tuple[ProcessTreeResourceSample, ...],
    pressure_before: CgroupPressureSample | None,
    pressure_after: CgroupPressureSample | None,
) -> ProcessResourceSummary:
    peak_rss = _max_available(sample.rss_bytes for sample in samples)
    peak_swap = _max_available(sample.swap_bytes for sample in samples)
    attribution_available = any(sample.attribution_available for sample in samples)
    return ProcessResourceSummary(
        peak_rss_bytes=peak_rss,
        peak_swap_bytes=peak_swap,
        memory_oom_events_delta=_delta(
            None if pressure_before is None else pressure_before.memory_oom_events,
            None if pressure_after is None else pressure_after.memory_oom_events,
        ),
        memory_oom_kill_events_delta=_delta(
            None if pressure_before is None else pressure_before.memory_oom_kill_events,
            None if pressure_after is None else pressure_after.memory_oom_kill_events,
        ),
        swap_current_bytes_delta=_delta(
            None if pressure_before is None else pressure_before.swap_current_bytes,
            None if pressure_after is None else pressure_after.swap_current_bytes,
        ),
        cpu_throttled_events_delta=_delta(
            None if pressure_before is None else pressure_before.cpu_throttled_events,
            None if pressure_after is None else pressure_after.cpu_throttled_events,
        ),
        cpu_throttled_usec_delta=_delta(
            None if pressure_before is None else pressure_before.cpu_throttled_usec,
            None if pressure_after is None else pressure_after.cpu_throttled_usec,
        ),
        attribution_available=attribution_available,
    )


def _read_parent_map(proc_root: Path) -> dict[int, int] | None:
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return None
    parent_map: dict[int, int] = {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        ppid = _read_ppid(entry / "stat")
        if ppid is not None:
            parent_map[int(entry.name)] = ppid
    return parent_map


def _read_ppid(stat_path: Path) -> int | None:
    try:
        text = stat_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        after_name = text.rsplit(")", maxsplit=1)[1].strip()
        fields = after_name.split()
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def _descendant_pids(root_pid: int, parent_map: dict[int, int]) -> tuple[int, ...]:
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parent_map.items():
            if pid not in selected and ppid in selected:
                selected.add(pid)
                changed = True
    return tuple(sorted(pid for pid in selected if pid in parent_map))


def _read_process_usage(status_path: Path) -> tuple[int, int] | None:
    try:
        lines = status_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    values: dict[str, int] = {}
    for line in lines:
        key, separator, raw_value = line.partition(":")
        if not separator or key not in {"VmRSS", "VmSwap"}:
            continue
        parts = raw_value.split()
        if not parts:
            continue
        try:
            values[key] = int(parts[0]) * 1024
        except ValueError:
            return None
    return values.get("VmRSS", 0), values.get("VmSwap", 0)


def _read_keyed_int_file(path: Path) -> dict[str, int] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    values: dict[str, int] = {}
    for line in lines:
        try:
            key, value = line.split(maxsplit=1)
            values[key] = int(value)
        except ValueError:
            return None
    return values


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _delta(before: int | None, after: int | None) -> int | None:
    if before is None or after is None:
        return None
    return max(0, after - before)


def _max_available(values: Iterable[int | None]) -> int | None:
    available = [value for value in values if isinstance(value, int)]
    return max(available) if available else None
