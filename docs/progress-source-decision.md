# Av1an Progress Source Decision

**Status:** Adopted
**Date:** 2026-07-14  
**Related fixtures:** `tests/fixtures/av1an_progress/`  
**Av1an tested:** `av1an 0.5.2-unstable` from the Avarch Docker runtime

## Decision

Use Av1an 0.5.2 TTY progress records as the numeric encoding progress
source, guarded by Av1an version/capability checks and backed by phase-only
fallback behavior.

The parser accepts only the captured Av1an 0.5.x progress
shape:

```text
00:00:00 [0/1 Chunks] ... 120/120 (519.30 fps, eta 0s, ...)
```

The source provides:

```text
current: current frame count from the progress record
total: total frame count from the same progress record
unit: frames
rate: fps value from the same progress record, when present
```

Chunk counts may be captured as status message context, but Avarch must not turn
chunk counts into an overall weighted job percentage.

## Rejected Alternatives

### Av1an Structured Progress

No structured or machine-readable progress stream was found in the tested
runtime. The fixture captures show no JSON or stable line protocol emitted while
encoding.

### Av1an Non-TTY Output

Non-TTY stdout is empty for the representative encode. Non-TTY stderr contains
phase/setup lines but no numeric in-flight progress:

```text
Scene detection
Queue 1 Workers 1 Encoder svt-av1 Passes 1
Params: --preset 10 --crf 55
```

This remains useful for phase heartbeat signals, but it cannot provide
`current`, `total`, or `rate`.

### Av1an State Files As Primary Numeric Source

Av1an writes stable temp files such as:

```text
chunks.json
done.json
scenes.json
```

`scenes.json` and `done.json` expose total frames and completed chunk state, and
they are useful for resume context and cross-checking. They do not expose
in-flight frame advancement or encoder rate while a chunk is running. For a
single long chunk, they can remain unchanged until the chunk completes.

### Generic Cosmetic Output Parsing

The implementation must not parse arbitrary Av1an console output. It may parse
only the specific progress records captured for the supported 0.5.x runtime.
Unsupported formats must disable numeric progress and keep phase/heartbeat
observability.

## Version And Capability Guard

Numeric parsing is enabled only for the supported Av1an family already recorded
in plans:

```text
0.5.x
```

The fixtures were captured from Docker `av1an 0.5.2-unstable`. If a future Av1an
version changes the progress record shape, Avarch should continue encoding but
fall back to phase-only progress until new fixtures and parser tests are added.

## Resume And Retry Behavior

Progress remains attempt-scoped.

A retry creates a new attempt and a separate progress row. Values from a failed
or cancelled attempt must never be merged into a later attempt.

For resume mode, Avarch may use Av1an state files to understand completed
chunks, but numeric frame progress still comes from observed progress records
for the current process. If a resumed run has no remaining chunks, the attempt
should publish phase and terminal snapshots rather than fabricate a new numeric
series.

## Fallback Behavior

If parsing fails, if the Av1an version is unsupported, or if progress records are
absent, Avarch publishes:

```text
phase: encoding
heartbeat_at: latest process activity or controlled runner heartbeat
current/total/rate: unknown
```

Encoding success or failure must not depend on telemetry success. Full
diagnostics remain in the raw stdout/stderr logs.

## Limitations

Avarch exposes phase progress, not overall job progress. Encoding
percentage is the current encoding phase percentage only. Concatenation, muxing,
validation, and promotion have phase/elapsed/heartbeat visibility unless a later
source provides trustworthy numeric progress for those phases.
