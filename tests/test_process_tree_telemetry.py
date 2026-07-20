from __future__ import annotations

from pathlib import Path

from avarch.adapters.system.process_telemetry import (
    LinuxProcessTreeSampler,
    read_cgroup_pressure,
)


def test_process_tree_sampler_aggregates_root_and_descendants(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    _write_proc(proc, pid=10, ppid=1, rss_kb=100, swap_kb=5)
    _write_proc(proc, pid=11, ppid=10, rss_kb=200, swap_kb=7)
    _write_proc(proc, pid=12, ppid=1, rss_kb=999, swap_kb=999)

    sample = LinuxProcessTreeSampler(proc_root=proc).sample(root_pid=10)

    assert sample.pids == (10, 11)
    assert sample.rss_bytes == 300 * 1024
    assert sample.swap_bytes == 12 * 1024
    assert sample.attribution_available is True


def test_process_tree_sampler_degrades_when_proc_is_unreadable(tmp_path: Path) -> None:
    sample = LinuxProcessTreeSampler(proc_root=tmp_path / "missing").sample(root_pid=10)

    assert sample.pids == ()
    assert sample.rss_bytes is None
    assert sample.swap_bytes is None
    assert sample.attribution_available is False
    assert sample.reason == "process_tree_unavailable"


def test_cgroup_pressure_reads_memory_and_cpu_events(tmp_path: Path) -> None:
    (tmp_path / "memory.events").write_text("low 0\noom 2\noom_kill 1\n", encoding="utf-8")
    (tmp_path / "memory.swap.current").write_text("4096\n", encoding="utf-8")
    (tmp_path / "cpu.stat").write_text(
        "usage_usec 1\nnr_throttled 3\nthrottled_usec 900\n",
        encoding="utf-8",
    )

    pressure = read_cgroup_pressure(tmp_path)

    assert pressure.memory_oom_events == 2
    assert pressure.memory_oom_kill_events == 1
    assert pressure.swap_current_bytes == 4096
    assert pressure.cpu_throttled_events == 3
    assert pressure.cpu_throttled_usec == 900


def _write_proc(
    proc: Path,
    *,
    pid: int,
    ppid: int,
    rss_kb: int,
    swap_kb: int,
) -> None:
    pid_dir = proc / str(pid)
    pid_dir.mkdir()
    (pid_dir / "stat").write_text(
        f"{pid} (python) S {ppid} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n",
        encoding="utf-8",
    )
    (pid_dir / "status").write_text(
        f"Name:\tpython\nVmRSS:\t{rss_kb} kB\nVmSwap:\t{swap_kb} kB\n",
        encoding="utf-8",
    )
