# Encoding Progress Implementation Baseline

**Status:** Recorded  
**Recorded:** 2026-07-14  
**Baseline commit:** 12998317d1aef6f176827f77502c95d1dfa33671  
**Branch:** feat/job-progress-observability

## Validation Commands

Configured project validation commands:

```sh
uv run pytest
uv run ruff check .
uv run pyright
docker build -t avarch-progress-baseline .
```

Baseline results:

```text
Full suite result: 708 passed, 4 skipped, 14 warnings in 263.12s
Ruff result: All checks passed
Pyright result: 0 errors, 0 warnings, 0 informations
Docker build result: passed, image sha256:66e4f6a9bb8514214e2458fcbb9126e5aa7feb72d8c724c7a1a10a1e81f580d9
```

## Runtime And Schema

```text
Current schema revision: 0005_job_state_version
Existing CLI framework: Typer 0.26.7
Existing terminal rendering dependency: Rich 15.0.0
Supported Av1an family in planning: 0.5.x
Docker Av1an version: av1an 0.5.2-unstable
Docker FFmpeg version: ffmpeg 7.1.5-0+deb13u1
Host Av1an check: fails to load libvsscript.so
Host FFmpeg version: ffmpeg N-121908-g7018ce14df
```

The Docker runtime is the trustworthy baseline for Av1an behavior. The host
`av1an --version` command exists but fails before printing a version because
`libvsscript.so` is unavailable.

## Current Job CLI Behavior

In a freshly initialized empty workspace, `avarch jobs list` prints:

```text
ID  STATUS     STAGE     PRI  TRY  CONTROL  PROFILE          FILE
```

In the same workspace, `avarch jobs show 1` prints:

```text
Job not found: 1
```

Current job listing fields are status, stage, priority, attempt count, control
state, profile, and file name. There are no phase, progress, ETA, heartbeat, or
last-update fields.

Current `jobs show` output includes job metadata, control state, artifacts,
attempt history, and events. It has no dedicated progress section.

## Detached Scheduler Behavior

In a freshly initialized empty workspace, `avarch scheduler run -d` starts a
native detached scheduler subprocess and prints:

```text
Scheduler started
PID: <pid>
Log: <workspace>/.avarch/logs/scheduler.log
```

`avarch scheduler status` from another process reports the scheduler process,
lease state, runner id, queue counts, control requests, and active jobs. With no
pending jobs, the detached scheduler exits after the configured idle period and
status returns to stopped with an inactive lease.

The Docker-backed wrapper documentation still recommends foreground scheduler
execution for normal one-shot wrapper use; native `scheduler run -d` is the
implemented detached path.

## Current Process Log Behavior

Encode execution writes separate raw stdout and stderr log files for Av1an and
FFmpeg under the plan runtime directory. The existing process runner passes
these file handles directly to `subprocess.Popen`, writes process start/end
markers to both logs, and reads failure tails from the stderr log. It does not
stream process output through Avarch code while the child is running.

Scheduler-level detached output is written to `.avarch/logs/scheduler.log`.
