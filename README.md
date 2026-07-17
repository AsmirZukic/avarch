# Avarch

Avarch is a local-first media archival tool that turns media files into smaller,
reproducible encodes while keeping the originals safe. It combines configurable
profiles, first-class VapourSynth scripts, Av1an encoding, FFmpeg muxing,
validation, and controlled output promotion.

Avarch is designed for media collections you keep on your own machine. It stores
project state next to the media in a local `.avarch` workspace and does not use a
hosted service, account, or subscription.

Highlights:

- Make media files smaller without replacing the originals.
- Full profile control over encoder, codec, quality target, preset, resolution,
	pixel format, HDR handling, audio, subtitles, output container, validation,
	and promotion policy.
- Custom VapourSynth `.vpy` templates and custom Python filter scripts are
	supported extension points.
- Reusable encoding profiles, including the built-in `av1_1080p_sdr` profile
	and the built-in `default` alias.
- Inspectable generated VapourSynth scripts and persisted plan artifacts.
- Av1an-based video encoding with SVT-AV1.
- FFmpeg muxing, metadata copying, chapter copying, audio transcoding, and
	subtitle stream copying.
- Scheduler-managed encoding with persistent workspace job state.
- Validation before completed output can be promoted.
- Project-local `.avarch` workspace state.
- No hosted service, account, or subscription.

## Audience Entry Points

**Just want smaller files?**
Jump to the [5-Minute Quick Start](#5-minute-quick-start).

**Want full control over your encode pipeline?**
Jump to [Profiles and VapourSynth Templates](#profiles-and-vapoursynth-templates).

## Alpha Safety Callout

**Avarch is currently in v0.1-alpha.** The core encoding workflow is available,
and your source media remains safe: Avarch builds and validates new output
separately instead of encoding over the original. Some commands, configuration
fields, and workspace formats may still change before the first stable release.

## Installation

### Requirements

For the current alpha, users need:

- Linux or a Linux-like environment with `sh`.
- Docker installed and running when using the provided `bin/avarch` wrapper.
- Enough storage for source media, temporary encode output, validation reports,
	and promoted output.

Avarch ships a Docker-backed CLI wrapper. The one-command installer downloads
that wrapper, configures it to use the published runtime image, and installs an
`avarch` command locally.

### One-Command Installation

Install the Docker-backed wrapper:

```sh
curl -fsSL https://raw.githubusercontent.com/AsmirZukic/avarch/main/scripts/install.sh | sh
```

The installer writes `avarch` to `$HOME/.local/bin` by default and pulls the
runtime image `docker.io/asmir100/avarch:alpha`. If `$HOME/.local/bin` is not
on your `PATH`, add it before running Avarch.

The installer does not build Avarch from source on the user's machine. It only
downloads the wrapper, pulls the selected Docker image, and configures the
installed command to run that image.

Install a semver-tagged image by setting `AVARCH_VERSION`:

```sh
curl -fsSL https://raw.githubusercontent.com/AsmirZukic/avarch/main/scripts/install.sh | \
	AVARCH_VERSION=0.1.0 sh
```

Use a full custom image by setting `AVARCH_IMAGE` for the install command:

```sh
curl -fsSL https://raw.githubusercontent.com/AsmirZukic/avarch/main/scripts/install.sh | \
	AVARCH_IMAGE=docker.io/YOUR_DOCKERHUB_USERNAME/avarch:latest sh
```

Current alpha setup from a checked-out copy of this repository still works:

```sh
docker build -t avarch:latest .
export PATH="$PWD/bin:$PATH"
avarch --version
```

`avarch --version` should print:

```text
0.1.0
```

### What Gets Installed

The installer writes only the wrapper command. The wrapper:

- Provides the `avarch` command from `bin/avarch`.
- Uses the installed default runtime image unless `AVARCH_IMAGE` overrides it.
- Preserves user files and existing workspaces when the wrapper or runtime image
	is rebuilt.
- Leaves workspace data under each project's `.avarch` directory.

`avarch init --force` is different: it removes and recreates the current
workspace's `.avarch` directory.

### Updating, Version Selection, and Uninstalling

Automated update and uninstall commands are not implemented yet. Re-run the
installer to refresh the wrapper and pull the configured image again.

Implemented version selection uses `AVARCH_IMAGE`:

```sh
AVARCH_IMAGE=docker.io/asmir100/avarch:0.1.0 avarch --version
```

To stop using the wrapper, remove the installed `avarch` command from your
install directory, or remove the repository `bin/` directory from `PATH` when
using a checked-out copy. Existing `.avarch` workspaces and media files are not
removed by that action.

### Inspect-Before-Running Alternative

To inspect the installer and launcher before using them:

```sh
curl -fsSL https://raw.githubusercontent.com/AsmirZukic/avarch/main/scripts/install.sh | less
less bin/avarch
sh bin/avarch --version
```

## 5-Minute Quick Start

The one-shot workflow command runs the normal scan, probe, plan, enqueue,
scheduler, validation, and promotion sequence for a workspace.

From the media directory you want Avarch to manage:

```sh
cd /path/to/media
avarch init
avarch doctor
avarch workflow run --profile av1_1080p_sdr --mode keep-original .
```

Without `--confirm`, promotion is previewed after validation so you can inspect
the final filesystem action. To promote in the same run, pass `--confirm` and
choose the promotion mode explicitly:

```sh
avarch workflow run --profile av1_1080p_sdr --mode replace-atomic --confirm .
```

Confirmed `replace-atomic` workflow runs allow the scheduler to promote and
clean each file as soon as it passes validation and size policy. Other modes are
promoted after the scheduler finishes so the requested final filesystem action
can be applied explicitly.

Promotion modes are `keep-original`, `move-original-to-backup`, and
`replace-atomic`. The source file is not encoded in place; promotion happens only
after validation passes.

For manual control, run the individual stages directly:

```sh
avarch scan .
avarch probe
avarch plan --profile av1_1080p_sdr
avarch enqueue
avarch scheduler run
```

`avarch scheduler run` opens the interactive owner dashboard. Press `d` or `q`
to detach while the scheduler keeps encoding; run `avarch scheduler watch` to
reattach later. Native execution can also use `avarch scheduler run -d`.

In another terminal in the same workspace, confirm that work is running:

```sh
avarch scheduler status
avarch jobs list
```

Example shape while an encode is active:

```text
Scheduler: running
State:     running
Lease:     active

Queue:
	queued:              0
	encoding:            1
	encoded:             0
	validating:          0
	ready to promote:    0
	promoting:           0
	promoted:            0
	size rejected:       0
	validation failed:   0
	failed:              0

Active:
	1   encode    movie.mkv
```

```text
ID  STATUS    STAGE   PRI  TRY  CONTROL  PROFILE          FILE
1   encoding  encode    0    1  -        av1_1080p_sdr   movie.mkv
```

The current status commands show file, state, stage, and profile. They do not yet
show a percentage progress field. Use `avarch jobs logs JOB_ID` to inspect the
latest stored attempt logs.

That is enough to start using Avarch. The following sections explain what Avarch
created, how the encoding pipeline is controlled, and how completed output is
validated and promoted.

## How Avarch Works

Avarch's workflow is:

```text
discover -> probe -> plan -> encode -> mux -> validate -> promote
```

Discovery finds source media and records file identity in the workspace. Probing
runs `ffprobe` and stores technical media information. Planning combines the
probe data with a selected profile and writes reviewable artifacts, including the
generated VapourSynth script. VapourSynth prepares video frames, Av1an manages
video encoding, and FFmpeg muxes the encoded video with selected audio,
subtitles, metadata, and chapters.

Validation checks the resulting file before promotion is allowed. Promotion then
places accepted output in its final location according to the selected mode. The
source file is not encoded in place.

## Profiles and VapourSynth Templates

Avarch's built-in workflow is configurable rather than closed. Profiles define
the archival policy, while VapourSynth templates and filters define the
frame-processing pipeline.

### Profiles

Profiles are TOML documents with `schema_version = 1`. They control these
implemented fields:

- `backend`: currently `av1an`.
- `container`: currently `mkv`.
- `match.video_codec_not`: skip sources whose primary video codec matches.
- `video.max_width`: even target width ceiling.
- `video.hdr_to_sdr`: opt into generated HDR-to-SDR handling.
- `video.source`: currently `vapoursynth`.
- `av1an.encoder`: currently `svt-av1`.
- `av1an.workers`: Av1an worker count for the job. Use an integer, or `"auto"`
  for a conservative SVT-AV1 worker count capped by CPU and memory.
- `av1an.video_args`: encoder argument string.
- `audio.codec`, `audio.bitrate`, `audio.channels`, `audio.languages`.
- `subtitles.languages` and `subtitles.keep_forced`.
- `vapoursynth.mode`: `generated`, `custom_filter`, or `custom_template`.
- `vapoursynth.script`, `vapoursynth.entrypoint`, `vapoursynth.template`, and
	`vapoursynth.api_version`.
- Validation thresholds: duration tolerance, minimum output size, minimum
	output/source ratio, optional decode sample, and optional minimum size
	reduction percent.

Built-in profiles in this alpha:

- `av1_1080p_sdr`: general-purpose 1080p SDR AV1 archival profile.
- `default`: built-in alias with the same settings as `av1_1080p_sdr`.

List profiles:

```sh
avarch profiles list
```

Inspect a built-in profile in the source tree:

```sh
less src/avarch/profiles/builtin/av1_1080p_sdr.toml
```

Copy a built-in profile into the workspace for editing:

```sh
avarch profiles copy av1_1080p_sdr --name my_archive
```

Custom profiles belong in the configured profile search path. The default search
path is `.avarch/profiles`, from `.avarch/config.toml`:

```toml
[profile_registry]
search_paths = ["profiles"]
```

Relative profile search paths are resolved from `.avarch/`, so the default is
`.avarch/profiles`.

Built-in name reservation is enforced. User profiles cannot use `default`,
`anime`, or `web_archive`. User profiles with other names can override visible
built-ins of the same name, but reserved names remain protected.

Profile validation happens when profiles are loaded and when related commands run.
Unknown profiles fail with messages such as `Unknown profile: missing`. Invalid
profile documents fail Pydantic validation and are rejected.

Profile changes affect new plans. Plans include a profile hash, probe hash,
execution identity hash, VapourSynth identity hash, and source filesystem
fingerprint. Existing queued jobs keep the plan artifact they were queued with;
running `avarch plan --profile NAME` after editing a profile creates a new plan
identity and supersedes earlier current plans for the same file/profile.

### First-Class Custom `.vpy` Templates

Custom VapourSynth templates are a supported extension point, not a workaround.
Templates and filter scripts live under `.avarch/scripts` by default.

Create a template scaffold:

```sh
avarch vpy scaffold template --name my_template
```

Point a profile at it:

```toml
[vapoursynth]
mode = "custom_template"
template = "my_template.vpy"
api_version = 1
```

Avarch prepends these variables to custom templates:

- `AVARCH_SOURCE_PATH`
- `AVARCH_VIDEO_STREAM_INDEX`
- `AVARCH_TARGET_WIDTH`
- `AVARCH_TARGET_HEIGHT`
- `AVARCH_INDEX_CACHE_DIR`
- `AVARCH_PLAN_HASH`
- `AVARCH_PROFILE_NAME`

Minimal custom template:

```python
from __future__ import annotations

from pathlib import Path

import vapoursynth as vs

core = vs.core

index_cache_dir = Path(AVARCH_INDEX_CACHE_DIR)
index_cache_dir.mkdir(parents=True, exist_ok=True)

clip = core.bs.VideoSource(source=AVARCH_SOURCE_PATH, cachepath=str(index_cache_dir))
clip = core.resize.Spline36(
		clip,
		width=AVARCH_TARGET_WIDTH,
		height=AVARCH_TARGET_HEIGHT,
		format=vs.YUV420P10,
)
clip.set_output(index=0)
```

Custom filter scripts use `mode = "custom_filter"` and expose a synchronous
entrypoint, defaulting to `apply(video, context)`. The generated script loads the
source with BestSource, builds an `avarch.vpy_api.FilterContext`, calls your
entrypoint, requires a `vs.VideoNode`, resizes to the planned output dimensions,
and registers output index 0.

Rendered scripts are stored in the plan artifact directory:

```text
.avarch/data/plans/<work-key>/<source-stem>.vpy
```

Inspect the generated script after planning:

```sh
avarch plan --profile my_archive --file movie.mkv --check-vpy
avarch plans show PLAN_ID
less .avarch/data/plans/<work-key>/movie.vpy
```

`--check-vpy` runs `vspipe --info` after the generated script is written. The
separate command below validates the profile's VapourSynth script statically and
checks a rendered script for one tracked file at runtime:

```sh
avarch vpy validate --profile my_archive
avarch vpy check movie.mkv --profile my_archive
```

The runtime includes VapourSynth, BestSource through `vapoursynth-bestsource`,
and plugin inventory through `avarch vpy plugins`. Workspace Python dependencies
can be added to `.avarch/vpy/requirements.toml` through:

```sh
avarch vpy packages install PACKAGE --kind python
avarch vpy sync
```

`avarch vpy packages search QUERY` uses `vsrepo` when it is available. The
current environment sync records VSRepo package names in the lock data, but native
plugin installation and making non-Python plugin files available remain the
user's responsibility in this alpha.

### Power-User Workflow

One complete control-oriented flow:

```sh
avarch profiles copy av1_1080p_sdr --name my_archive
avarch vpy scaffold template --name my_template
```

Edit `.avarch/profiles/my_archive.toml` so it contains:

```toml
[vapoursynth]
mode = "custom_template"
template = "my_template.vpy"
api_version = 1
```

Then validate, render, inspect, and queue:

```sh
avarch vpy validate --profile my_archive
avarch scan .
avarch probe --file movie.mkv
avarch vpy check movie.mkv --profile my_archive
avarch plan --profile my_archive --file movie.mkv --check-vpy
avarch plans list
avarch plans show PLAN_ID
avarch enqueue --plan PLAN_ID
```

This workflow exposes the resolved profile, generated script, Av1an command JSON,
validation policy JSON, plan JSON, and queue identity before encoding begins.

## The `.avarch` Workspace

Avarch keeps its generated state inside `.avarch` so your media directory stays
self-contained. Most of this directory is maintained automatically, while
profiles, templates, and documented configuration files remain available for you
to customize.

### Files Avarch Manages For You

`avarch init` creates this workspace shape:

```text
.avarch/
	workspace.toml
	config.toml
	profiles/
	scripts/
	vpy/
		requirements.toml
		environments/
	data/
		avarch.db
	logs/
	work/
	tmp/
	run/
```

During normal operation Avarch also creates managed data under `.avarch/data`,
including:

- SQLite database: `.avarch/data/avarch.db`.
- Persisted plan rows and plan artifact paths.
- Plan artifact bundles under `.avarch/data/plans/<work-key>/`.
- Generated `.vpy` scripts inside each plan artifact bundle.
- Av1an command JSON and validation policy JSON.
- Encode work directories under `.avarch/data/work/<work-key>/`.
- Runtime logs, stage markers, encode receipts, and validation reports.
- Scheduler metadata under `.avarch/run/` and scheduler logs under
	`.avarch/logs/`.

Users normally do not need to edit these directly. The SQLite database is an
implementation detail and should normally be accessed through Avarch commands
rather than edited manually.

### Files Intended For User Editing

Implemented editable workspace files and directories:

- `.avarch/config.toml`
- `.avarch/profiles/*.toml`
- `.avarch/scripts/*.py`
- `.avarch/scripts/*.vpy`
- `.avarch/vpy/requirements.toml`

There is no separate ignore-rule file yet. Scan exclusions are configured with
`scanner.exclude_directories` in `.avarch/config.toml`.

### Workspace Lifecycle

`avarch init` creates `.avarch`, writes `workspace.toml`, writes
`config.toml`, creates `vpy/requirements.toml`, creates the default directory
tree, and upgrades the SQLite database to the current migration revision.

If `.avarch` already exists, `avarch init` refuses to overwrite it. Use
`avarch init --force` only when you intentionally want to delete and recreate
the workspace state, including custom profiles and scripts stored inside
`.avarch`.

Workspace lookup searches upward from the current directory for
`.avarch/workspace.toml`, so Avarch commands can be run from nested folders
inside a workspace.

Backups are currently manual. Back up your media and the `.avarch` directory if
you want to preserve plans, probe cache, queue state, custom profiles, custom
scripts, and validation records.

Version control recommendations:

- Consider tracking `.avarch/config.toml`, `.avarch/profiles/`,
	`.avarch/scripts/`, and `.avarch/vpy/requirements.toml` if the workspace is
	project-like and the files do not contain local-only paths.
- Usually ignore `.avarch/data/`, `.avarch/logs/`, `.avarch/run/`,
	`.avarch/tmp/`, `.avarch/work/`, and `.avarch/vpy/environments/`.

Media scanned inside the workspace is stored with paths relative to the workspace
root, so moving the whole media directory with `.avarch` preserves inventory
paths. Media scanned from outside the workspace can be stored as absolute paths.
Use separate `avarch init` workspaces for independent libraries.

## Scanning, Probing, and File Identity

`avarch scan ROOT` recursively walks each root with `followlinks=False`. It skips
symlinked directories, symlinked files, `.avarch` by default, and files ending in
`.avarch-original`.

Default scanned extensions are case-insensitive:

```text
.mkv .mp4 .m4v .mov .avi .webm .ts .m2ts
```

Configure scan roots and extensions in `.avarch/config.toml`:

```toml
[scanner]
roots = []
extensions = [".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts"]
exclude_directories = [".avarch"]
```

Scan records file status as `added`, `present`, `changed`, or `missing`.
`avarch files list --changed` shows added, changed, and missing files only.

Avarch's file freshness key is a filesystem fingerprint derived from path, size,
mtime nanoseconds, device, and inode. It is a cheap freshness check, not a
cryptographic content hash.

`avarch probe` runs `ffprobe -print_format json -show_format -show_streams
-show_chapters` for active scanned files with missing or stale probes. Use
`--force` to probe even when the cached probe is current.

When a source file changes, Avarch invalidates older probe information by
fingerprint instead of assuming the previous plan is still safe.

Bulk probe:

```sh
avarch probe
```

Single-file probe:

```sh
avarch probe --file movie.mkv --force
```

Inspect one tracked file:

```sh
avarch files show --file movie.mkv
```

## Plans and Execution Identity

A plan is the resolved intent for one source file and one profile. It contains:

- Source path and source filesystem fingerprint.
- Probe hash and profile hash.
- Video target codec, dimensions, pixel format, and VapourSynth mode.
- Audio and subtitle stream decisions.
- Generated VapourSynth script path.
- Av1an command specification.
- FFmpeg mux command specification.
- Validation policy.
- Promotion policy.
- Runtime and artifact paths.
- Execution identity hash.

Plans are persisted in the database and written as JSON under:

```text
.avarch/data/plans/<work-key>/plan.json
```

The same directory includes:

```text
<source-stem>.vpy
av1an.command.json
validation-policy.json
vpy/environment-lock.toml
vpy/snapshot.json
```

Inspect plans:

```sh
avarch plans list
avarch plans show PLAN_ID
```

A plan becomes stale when the source filesystem fingerprint, probe hash, profile
hash, or execution identity no longer matches the current workspace state. New
plans supersede previous current plans for the same file/profile. Queued jobs use
the plan artifact recorded on the job.

Execution identity captures command-contract details such as Av1an contract
version, FFmpeg mux contract version, expected Av1an version family, final
container, no-overwrite behavior, and mux policy. Duplicate work is avoided by
plan hashes and queue keys; attempting to enqueue the same current plan again is
reported as already queued or already completed.

## Scheduler and Job Management

The scheduler runs queued jobs through `probe`, `plan`, `scene_detect`, `encode`,
`validate`, size policy, cleanup, and `promote`. Completed encodes do not wait for the full
queue: one job can be validating or promoting while another job is still
encoding, subject to the configured resource limits.

Scheduler commands:

```sh
avarch scheduler run
avarch scheduler run -d
avarch scheduler status
avarch scheduler watch
avarch scheduler watch --once
avarch scheduler watch --json
avarch scheduler pause --reason "maintenance"
avarch scheduler resume
avarch scheduler drain --wait
avarch scheduler stop --wait
avarch scheduler stop --force
avarch scheduler restart
```

`scheduler run -d` is implemented for native host execution. The Docker wrapper
starts interactive `scheduler run` work in its named encoder container and opens
the owner dashboard as a separate control surface, so detaching leaves the
encoder container running.

`scheduler run` owns scheduler execution. It claims jobs, starts workers,
persists heartbeats, and responds to control requests. `scheduler watch` is an
observer and control surface for an already-initialized workspace; it polls
short read-only snapshots, renders scheduler state, and sends pause/resume or
job-cancel requests through the same scheduler/job control stores used by the
normal CLI commands.

Interactive `avarch scheduler watch` requires a TTY. In a terminal it shows the
scheduler dashboard with pipeline counts, active jobs, upcoming work, capacity,
recent lifecycle events, resource telemetry, and honest queue forecasts when
enough comparable encode history exists. Keyboard shortcuts are Linux/POSIX
first: `p` pauses or resumes, `c` opens a bounded cancellation confirmation for
the selected active job, `l` toggles a bounded log tail, Enter toggles details,
and `d` or `q` detaches. Ctrl+C detaches from watch mode; it does not stop the
scheduler. In the owner dashboard opened by `scheduler run`, Ctrl+C requests a
scheduler stop.

For scripts and non-interactive shells, use:

```sh
avarch scheduler watch --once
avarch scheduler watch --json
avarch scheduler watch --once --no-color
```

Live watch also accepts `--interval SECONDS`. Non-TTY live mode exits with
guidance to use `--once` or `--json`; it does not emit cursor-control output.
The Docker wrapper preserves TTY flags for foreground interactive commands, so
`avarch scheduler watch` works normally from an interactive shell. Direct Docker
usage must pass `-it` for live watch, for example:

```sh
docker run --rm -it -v "$PWD:/workspace" -w /workspace avarch:latest scheduler watch
```

On platforms without supported nonblocking keyboard input, the dashboard remains
usable as a live observer and reports that interactive shortcuts are unavailable.
Resource telemetry is Linux-first: CPU and memory use Linux `/proc` and cgroup
data when available, and active output write rate measures growth of known
attempt output/temp files. It is informational and is not reported as physical
disk throughput.

Job states:

| State | Meaning |
| --- | --- |
| `queued` | Ready to be claimed by the scheduler. |
| `encoding` | Claimed for encode work. |
| `encoded` | Encode finished and validation is next. |
| `validating` | Validation or validation recovery is active. |
| `validation_failed` | Output failed required validation checks. |
| `size_rejected` | Output passed validation but failed size policy and was cleaned up or retained according to profile policy. |
| `ready_to_promote` | Output passed validation and size policy. |
| `promoting` | Promotion or promotion recovery is active. |
| `promoted` | Job finished after promotion. |
| `held` | Held before continuing to the next stage. |
| `failed` | Current stage failed. |
| `cancelled` | Cancelled by user request. |
| `skipped` | Skipped, for example because the profile did not match or a duplicate plan exists. |

Job stages:

| Stage | Resource class |
| --- | --- |
| `probe` | cheap |
| `plan` | cheap |
| `encode` | heavy_av1an |
| `validate` | cheap |
| `promote` | file_op |

Preparation and validation stages may overlap up to `resources.cheap_workers`.
Heavy encodes run up to `resources.av1an_jobs`; the default is one Av1an job.
Promotion uses `resources.file_ops`. These independent limits allow per-job
validation, cleanup, and promotion to continue while unrelated encodes run.

Job commands:

```sh
avarch jobs list
avarch jobs list --status failed --stage validate --limit 10
avarch jobs show JOB_ID
avarch jobs watch JOB_ID
avarch jobs logs JOB_ID --tail-bytes 16384
avarch jobs cancel JOB_ID --reason "wrong profile" --wait
avarch jobs cancel --running --reason "shutdown"
avarch jobs hold JOB_ID --reason "review first"
avarch jobs release JOB_ID
avarch jobs retry JOB_ID
avarch jobs retry --failed
avarch jobs priority JOB_ID 10
avarch jobs clear --completed --confirm
avarch jobs clear --failed --confirm
avarch jobs validate JOB_ID
```

Recovery after restart is state-aware. `encoded` and `validating` jobs resume
validation if the encoded output still exists, missing encoded outputs fail the
job without touching the original, `ready_to_promote` jobs remain promotable,
and `promoting` jobs are resumed through promotion recovery. Startup recovery
never blindly deletes files.

Job progress is persisted per attempt so detached scheduler work can be
inspected from another shell. `jobs list` shows compact phase, phase progress,
ETA, and last-update fields. `jobs show` adds the current attempt id, phase
progress, elapsed time, ETA, speed/rate when known, source, heartbeat age, last
measurable advancement, and a bounded status message. `jobs watch JOB_ID` polls
SQLite and renders a live Rich display on an interactive terminal; redirected
output is plain complete lines with no cursor-control sequences. `NO_COLOR=1`
disables color.

Interactive `scheduler run` starts the scheduler independently and opens the
same live progress view in owner mode. Its footer exposes pause/resume,
confirmed cancellation, logs, details, detach, and stop controls. Detached
scheduler runs do not render live progress; use `scheduler watch` or
`jobs watch JOB_ID` from another shell.

Progress percentages are phase-specific, not whole-workflow percentages. Unknown
totals show the current value when available and `eta=unknown`; Avarch does not
fabricate `0%` or an ETA. Heartbeat means the owned process or scheduler path was
recently observed; last advancement means numeric progress moved. A job can have
a recent heartbeat while not advancing, and stale heartbeat reporting does not
mark the job failed by itself.

Numeric Av1an progress is enabled for the supported 0.5.x TTY progress format.
Unsupported versions, unparsable output, legacy jobs without progress rows, or
jobs without a numeric total fall back to phase/heartbeat visibility while the
raw stdout/stderr logs remain complete. Ctrl+C while running `jobs watch` stops
only the watcher; use `jobs cancel` to request job cancellation through the
scheduler control path.

Cancellation requests for active non-promotion jobs cancel the scheduler task.
`scheduler stop` sends SIGTERM to the scheduler process group and escalates to
SIGKILL if needed. Running promotion transactions cannot be canceled through
`jobs cancel` because they require promotion recovery semantics.

## Validation and Promotion

Avarch keeps the plan -> validate -> size policy -> promote boundary explicit.
Encoded output is created in workspace work paths, validation and size policy
must pass, and promotion is the controlled step that places accepted output in
its final location.

Avarch never replaces the original unless the encoded file passes validation and
size policy.

### Validation

Validation runs after encode in the scheduler, or manually for eligible jobs:

```sh
avarch jobs validate JOB_ID
```

Validation checks include:

- Source fingerprint still matches the plan.
- Source remains stable during validation.
- Output exists, is a regular file, is non-empty, and remains stable.
- `ffprobe` can read the output.
- Duration is within profile tolerance.
- Container is accepted for the plan.
- Video stream count, codec, and resolution match the policy.
- Audio stream count, codec, channels, and language match the policy.
- Subtitle stream count and subtitle policy match the plan.
- Output meets minimum size rules.
- Optional minimum size reduction passes when configured.
- Optional decode sample passes when enabled.

Validation policy failures produce a `ValidationReport` with check statuses:
`pass`, `fail`, `warning`, or `skipped`. Required failed checks make the report
fail and leave the job in `validation_failed` at stage `validate`; corrupt
outputs never replace originals.

Validation execution errors are different from failed validation reports.
Missing tools, unreadable targets, subprocess failures, or persistence problems
raise validation error types such as `ValidationExecutionError`,
`ValidationTargetError`, `ValidationPolicyError`, or
`ValidationPersistenceError` and fail the job stage.

After fixing a validation-stage problem, retry with:

```sh
avarch jobs retry JOB_ID
avarch jobs validate JOB_ID
```

Failed validation does not delete the source file. Encoded output and logs are
left in the workspace work/artifact paths for inspection unless later cleanup is
performed by an explicit retry or operator action.

### Size Policy

After validation passes, Avarch applies the profile `[promotion]` size policy:

```toml
[promotion]
require_smaller = true
minimum_savings_percent = 5
delete_rejected_output = true
```

By default, outputs must be smaller and must save at least 5 percent. A file
that is only 0.1 percent smaller is rejected by default. `REJECT_NOT_SMALLER`
sets the outcome reason `skipped_size_not_smaller`; insufficient savings sets
`skipped_minimum_savings_not_met`. When `delete_rejected_output` is true, the
rejected encoded temp file is deleted and the original remains untouched. If
cleanup fails, the job is marked `failed` and the original is still untouched.

### Promotion

Promotion requires a job in `ready_to_promote` state at `promote` stage with a
latest passing validation result matching the plan and output path. The scheduler
can promote automatically; `avarch promote JOB_ID` remains available for manual
preview, execution, and recovery.

Preview promotion:

```sh
avarch promote JOB_ID --dry-run
```

Execute promotion:

```sh
avarch promote JOB_ID --confirm
```

Recover an interrupted promotion:

```sh
avarch promote JOB_ID --recover
```

Promotion modes:

| Mode | Final path | Original behavior |
| --- | --- | --- |
| `keep-original` | Sibling named `<stem>.av1.mkv` | Original remains in place. |
| `move-original-to-backup` | Original source path | Original is moved to `<original>.avarch-original`. |
| `replace-atomic` | Original source path | Uses hard-link rollback backup and atomic replacement requirements. |

Collision behavior is strict. Promotion fails if the final path, backup path, or
staging path already exists. Same-directory hidden staging files are used, for
example `.Movie.av1.mkv.avarch-promote-abcdef123456.tmp`.

Promotion verifies source and output fingerprints, locks the promotion target,
stages the validated output, hashes content with the `promotion-content-v1`
contract, verifies staging and final digests, journals progress, and can recover
interrupted transactions. A failed promotion releases its lock; a promoted job
stays terminal.

Cross-filesystem replacement is limited. The promotion policy requires hard-link
support for replacement-style modes and destination-local staging. Keep-original
mode writes a sibling final file and is the safest default.

Originals are never encoded over or deleted first. In `keep-original`, originals
are never moved. In replacement modes, Avarch creates or uses rollback/backup
handling, re-stats the original, verifies identity, installs only the validated
output, verifies the final path, and deletes the backup only after a safe
replacement exists.

Disk-space behavior is conservative. Validation and promotion need room for the
encoded output, promotion staging, and any rollback backup required by the
selected mode. Rejected outputs can be deleted immediately by policy to reclaim
space; successful replacement promotion cleans up known work paths after commit.

## Configuration

Generated workspace configuration lives at `.avarch/config.toml`:

```toml
[app]
data_dir = "data"

[database]
url = "sqlite:///data/avarch.db"

[logging]
level = "INFO"
format = "console"

[resources]
cheap_workers = 4
av1an_jobs = 1
file_ops = 1

[scanner]
roots = []
extensions = [".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts"]
exclude_directories = [".avarch"]

[profile_registry]
search_paths = ["profiles"]
```

Common user-editable settings:

- `logging.level`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`.
- `logging.format`: `console` or `json`.
- `resources.cheap_workers`: concurrent cheap stages.
- `resources.av1an_jobs`: concurrent Av1an encode stages.
- `resources.file_ops`: configured file-operation capacity.
- `scanner.roots`: roots used when `avarch scan` is run without arguments.
- `scanner.extensions`: extensions Avarch discovers.
- `scanner.exclude_directories`: directory names skipped while scanning.
- `profile_registry.search_paths`: profile search paths relative to `.avarch`
	unless absolute.

The workspace config controls discovery, resource limits, logging, database
location, and where profiles are loaded from. Individual profiles control encode,
stream, VapourSynth, validation, and promotion policy. Profile and execution
settings apply to new plans; existing plan artifacts and queued jobs keep their
recorded identity.

## Command Reference

### Workspace

`avarch init [--force]`

Create `.avarch`, config, directories, and database. `--force` recreates the
workspace.

```sh
avarch init
```

`avarch workspace info`

Show workspace paths.

```sh
avarch workspace info
```

`avarch config show`

Show resolved config paths.

```sh
avarch config show
```

`avarch db current` and `avarch db upgrade`

Show or apply the database migration revision.

```sh
avarch db current
avarch db upgrade
```

### Discovery and Probing

`avarch scan [ROOTS...]`

Discover media files under roots, or under configured roots when none are passed.

```sh
avarch scan .
```

`avarch files list [--changed]`

List tracked files; `--changed` limits output to added, changed, and missing.

```sh
avarch files list --changed
```

`avarch files show --file PATH`

Show one tracked file, its probe hash, and current plan hash.

```sh
avarch files show --file movie.mkv
```

`avarch probe [--file PATH] [--force]`

Run `ffprobe` for missing or stale probes.

```sh
avarch probe --file movie.mkv --force
```

### Profiles

`avarch profiles list`

List built-in and user profiles.

```sh
avarch profiles list
```

`avarch profiles copy SOURCE --name NAME`

Copy a built-in profile into the user profile directory.

```sh
avarch profiles copy av1_1080p_sdr --name my_archive
```

`avarch profiles scaffold --from SOURCE --name NAME [--filter FILTER]`

Copy a profile and optionally scaffold a filter script.

```sh
avarch profiles scaffold --from av1_1080p_sdr --name tuned --filter tuned_filter.py
```

### Planning

`avarch plan --profile NAME [--file PATH] [--force] [--check-vpy/--no-check-vpy]`

Create persisted plans and generated VapourSynth scripts for current successful
probes.

```sh
avarch plan --profile av1_1080p_sdr --file movie.mkv --check-vpy
```

`avarch plans list [--current/--all]`

List current valid plans by default.

```sh
avarch plans list --all
```

`avarch plans show PLAN_ID_OR_HASH`

Show plan metadata and artifact path.

```sh
avarch plans show 1
```

### Scheduler

`avarch enqueue [--file PATH] [--plan PLAN_ID_OR_HASH] [--priority INT]`

Queue current valid plans.

```sh
avarch enqueue --file movie.mkv --priority 5
```

`avarch scheduler run [--resume] [-d|--detached]`

Run the scheduler in foreground or native detached mode.

```sh
avarch scheduler run
```

`avarch scheduler status`

Show scheduler process state, lease state, queue counts, and active jobs.

```sh
avarch scheduler status
```

`avarch scheduler pause|resume|drain|stop|restart`

Control scheduler lifecycle.

```sh
avarch scheduler stop --wait
```

### Jobs

`avarch jobs list [--status STATUS] [--stage STAGE] [--profile NAME] [--limit N]`

List queued and historical jobs with compact progress columns:

```text
ID  STATUS  PHASE  PROGRESS  ETA  UPDATED  PROFILE  FILE
```

```sh
avarch jobs list --status failed
```

`avarch jobs show JOB_ID`

Show job details, current progress, artifacts, attempts, and events.

```sh
avarch jobs show 1
```

`avarch jobs watch JOB_ID [--poll-interval SECONDS]`

Watch persisted progress until the job reaches a terminal or handoff state.
Interactive terminals use a live Rich display. Non-TTY output is line-oriented,
for example:

```text
episode-01.mkv encoding 67.1% elapsed=42m 18s eta=20m 51s speed=1.32x
```

Completed, promoted, skipped, ready-to-promote, and size-rejected terminal or
handoff states exit successfully. Failed, validation-failed, and cancelled jobs
show their final state and exit non-zero. Ctrl+C exits with status 130 and does
not cancel the job.

```sh
avarch jobs watch 1
avarch jobs watch 1 > progress.log
NO_COLOR=1 avarch jobs watch 1
```

`avarch jobs logs JOB_ID [--attempt N] [--tail-bytes N]`

Show stored stdout/stderr tail for an attempt.

```sh
avarch jobs logs 1 --tail-bytes 32768
```

`avarch jobs cancel [JOB_ID|--running] [--reason TEXT] [--wait]`

Request cancellation.

```sh
avarch jobs cancel 1 --reason "wrong profile" --wait
```

`avarch jobs hold JOB_ID`, `release JOB_ID`, `retry JOB_ID`, `retry --failed`,
`priority JOB_ID VALUE`, and `clear --completed|--failed --confirm`

Manage queued or failed work.

```sh
avarch jobs retry --failed
```

`avarch jobs validate JOB_ID`

Run validation for an eligible validation-stage job or reuse a current passed
validation.

```sh
avarch jobs validate 1
```

### Validation

Validation is normally run by the scheduler after encode. Manual validation uses
the job command:

```sh
avarch jobs validate JOB_ID
```

### Promotion

`avarch promote JOB_ID [--mode MODE] [--dry-run] [--confirm] [--recover]`

Preview, execute, or recover promotion. Modes are `keep-original`,
`move-original-to-backup`, and `replace-atomic`.

```sh
avarch promote 1 --mode keep-original --confirm
```

### Diagnostics

`avarch doctor [--log-format console|json]`

Verify config, database, app version, and runtime identity.

```sh
avarch doctor
```

`avarch vpy validate --profile NAME`

Run static validation for a profile's VapourSynth scripts.

```sh
avarch vpy validate --profile my_archive
```

`avarch vpy check FILE --profile NAME`

Render a script for a tracked file and run `vspipe --info`.

```sh
avarch vpy check movie.mkv --profile my_archive
```

`avarch vpy plugins`, `vpy env show`, `vpy env check`, `vpy packages list`,
`vpy packages search`, `vpy packages install`, and `vpy packages remove`

Inspect and manage workspace VapourSynth environment metadata.

```sh
avarch vpy env check
```

### Version and Help

```sh
avarch --version
avarch version
avarch --help
avarch COMMAND --help
```

## Troubleshooting

### Installation Failure

What you see: `avarch: not found`.

Likely cause: the repository `bin/` directory is not on `PATH`.

Recovery:

```sh
export PATH="/path/to/avarch/bin:$PATH"
avarch --version
```

### Docker Unavailable

What you see: Docker command errors from the wrapper.

Likely cause: Docker is not installed, not running, or the user cannot access it.

Recovery: start Docker, fix user permissions for Docker, then run:

```sh
avarch --version
```

### No Workspace Found

What you see: `No Avarch workspace found. Run 'avarch init' from the workspace root.`

Likely cause: the command was run outside a directory containing
`.avarch/workspace.toml` in its parent chain.

Recovery:

```sh
cd /path/to/media
avarch init
```

### Permission Problems

What you see: permission denied while initializing or writing workspace files.

Likely cause: the current directory is not writable by the invoking user or
container user.

Recovery: fix ownership or permissions, then retry `avarch init`. The wrapper
runs as your UID/GID.

### Source Not Discovered

What you see: `No scan roots provided or configured.` or the file is absent from
`avarch files list`.

Likely cause: no root was passed, the file extension is not configured, or the
file is under an excluded directory.

Recovery:

```sh
avarch scan .
avarch files list
```

### Stale Probe Result

What you see: `probe-stale` during planning.

Likely cause: the source file changed after its stored probe.

Recovery:

```sh
avarch scan .
avarch probe --file movie.mkv --force
avarch plan --profile av1_1080p_sdr --file movie.mkv
```

### Unknown or Invalid Profile

What you see: `Unknown profile: NAME` or invalid profile document errors.

Likely cause: profile name is misspelled, not in a configured search path, or the
TOML does not match schema version 1.

Recovery:

```sh
avarch profiles list
avarch profiles copy av1_1080p_sdr --name my_archive
```

### Missing VapourSynth Plugin

What you see: `vspipe` failure or plugin namespace errors.

Likely cause: the template uses a plugin not available in the runtime or
workspace environment.

Recovery:

```sh
avarch vpy plugins
avarch vpy packages install PACKAGE --kind python
avarch vpy sync
avarch vpy check movie.mkv --profile my_archive
```

Native plugin files remain the user's responsibility in this alpha.

### `.vpy` Rendering or Execution Failure

What you see: `VapourSynth artifacts were generated, but runtime validation failed.`

Likely cause: syntax passed but `vspipe --info` failed at runtime.

Recovery:

```sh
avarch vpy validate --profile my_archive
avarch plan --profile my_archive --file movie.mkv --check-vpy --force
```

### Encoder Unavailable

What you see: Av1an or encoder preflight failure.

Likely cause: native execution is missing `av1an` or `SvtAv1EncApp`, or the
runtime does not satisfy Av1an `0.5.x` expectations.

Recovery: use the built runtime image, or install the native media toolchain and
rerun:

```sh
avarch doctor
```

### Encoder Killed During Encoding

What you see: Av1an fails with `encoder crashed: signal: 9 (SIGKILL)`, often on
several chunks near the start of encoding.

Likely cause: the OS or container killed SVT-AV1 under memory pressure. Each
Av1an worker can run a separate SVT-AV1 encoder process, and SVT-AV1 also uses
internal parallelism.

Recovery: reduce Av1an workers and run from a newly planned artifact. The
built-in `"auto"` worker mode is conservative, but existing queued or failed
jobs keep the worker count recorded in their plan artifact.

```sh
# After updating the same profile or Avarch version, retrying can reset the job
# to planning when the old plan identity no longer matches.
avarch jobs retry JOB_ID

# Or create a separate lower-memory profile and enqueue a new job for it.
avarch profiles copy av1_1080p_sdr --name low_mem
# edit .avarch/profiles/low_mem.toml and set: workers = 2
avarch plan --profile low_mem --file movie.mkv --force
avarch enqueue --file movie.mkv
```

### Scheduler Already Running

What you see: `Scheduler is already running.`, `Another scheduler lease is still
active.`, or `Another scheduler process holds the workspace lock.`

Likely cause: a live scheduler already owns the workspace.

Recovery:

```sh
avarch scheduler status
avarch scheduler stop --wait
```

### Scheduler Not Running

What you see: `No live scheduler process exists.` when pausing or resuming.

Likely cause: there is no active scheduler process.

Recovery:

```sh
avarch scheduler run
```

### Failed or Interrupted Jobs

What you see: `failed`, `queued`, `encoded`, or `ready_to_promote` after an
interrupted attempt.

Likely cause: a stage failed, the scheduler stopped, or recovery returned a job
to a resumable state.

Recovery:

```sh
avarch jobs show JOB_ID
avarch jobs logs JOB_ID
avarch jobs retry JOB_ID
```

### Cancellation Problems

What you see: cancellation does not immediately finish or promotion jobs refuse
job cancellation.

Likely cause: non-promotion cancellation is cooperative through the scheduler;
promotion has separate recovery semantics.

Recovery:

```sh
avarch jobs cancel JOB_ID --wait
avarch promote JOB_ID --recover
```

Use the promotion recovery command only for interrupted promotion transactions.

### Validation Failure

What you see: `Validation failed required checks: ...`.

Likely cause: output did not meet the plan's validation policy.

Recovery:

```sh
avarch jobs show JOB_ID
avarch jobs logs JOB_ID
avarch jobs retry JOB_ID
```

### Promotion Collision

What you see: `Final path already exists`, `Backup path already exists`, or
`Staging path already exists`.

Likely cause: promotion would overwrite an existing file or stale staging file.

Recovery: inspect the path, move or remove only files you have confirmed are safe
to change, then preview again:

```sh
avarch promote JOB_ID --dry-run
```

### Insufficient Storage

What you see: `Insufficient free space for destination-local staging.`

Likely cause: promotion requires the validated output size plus a 64 MiB reserve
in the destination directory.

Recovery: free space on the destination filesystem and retry:

```sh
avarch promote JOB_ID --confirm
```

### Unsupported Platform

What you see: scheduler lifecycle or process metadata errors on non-Linux hosts.

Likely cause: this alpha uses Linux process features such as `/proc`, process
groups, and `fcntl` locks.

Recovery: run on Linux or in the supported container environment.

## What Avarch Is Not

Avarch is not:

- A media server.
- A download manager.
- A real-time transcoder.
- A video editor.
- A generic FFmpeg frontend.
- A hosted service.
- A distributed encoding platform.
- A completely hands-off decision engine.
- A provider of every VapourSynth plugin.

## Project Status and Alpha Limitations

Current version: `0.1.0` in code, documented here as `v0.1-alpha` product state.

Supported operating system status:

- Verified implementation targets Linux-style environments.
- The wrapper is POSIX `sh`.
- Scheduler lifecycle code uses Linux `/proc`, process groups, and `fcntl` locks.

Container/runtime status:

- The Dockerfile builds for the local Docker platform from `python:3.12-slim`.
- Python runtime is 3.12.
- `uv` version in the Dockerfile is `0.10.7`.
- SVT-AV1 is built from `v2.3.0` by default.
- Av1an is installed with requirement `>=0.5,<0.6`; the execution contract
	expects Av1an `0.5.x`.
- FFmpeg and FFprobe come from Debian packages and are not pinned to an exact
	version in the Dockerfile.
- VapourSynth is provided by the Python environment; `avarch doctor` and
	`avarch vpy env show` report the detected runtime version.

Schema and compatibility:

- Workspace schema version: `1`.
- Profile schema version: `1`.
- Transcode plan schema version: `5`.
- Validation policy schema version: `2`.
- Validation report schema version: `1`.
- Promotion policy schema version: `1`.
- Database migrations exist, but pre-release development schema compatibility is
	not guaranteed.

Practical alpha limitations:

- The public one-command installer installs the Docker-backed wrapper only; it
	does not install Docker itself.
- No single casual `encode` command combines scan, probe, plan, enqueue, and
	scheduler startup.
- `avarch init` does not scan automatically.
- Native detached mode and the Docker wrapper's named encoder container remain
	separate process-management paths.
- Promotion is explicit and not automatically run by the scheduler.
- Cross-filesystem replacement-style promotion is limited by hard-link and
	destination-local staging requirements.
- Native plugin installation for VSRepo packages is not fully automated by
	workspace environment sync.
- Built-in generated VapourSynth handling supports selected source formats and
	HDR-to-SDR policy, but does not deinterlace.
- Root-level `avarch.toml`, root-level `profiles/`, and `.avarch/avarch.db` are
	not supported workspace state.

Existing encoded files are separate media files. Source media is unaffected by
workspace format changes, but an alpha `.avarch` workspace may need to be backed
up, migrated manually, or recreated after incompatible development changes.

## Contributing and Development

From a checked-out repository:

```sh
uv sync
uv run avarch --version
```

Common development commands:

```sh
make test
make lint
make format
make typecheck
make check
```

CI runs the same checks on GitHub Actions: Ruff, Pyright, Pytest, and Docker
image build. Pull requests build the container without publishing it. Pushes to
the default branch and matching `v*` tags publish to Docker Hub when the Docker
Hub credentials are configured.

Publish runs fail CI if Docker Hub credentials are missing, login fails, or the
image push is rejected. Pull request builds are validation-only and load the
built image into the runner instead of pushing it.

Docker image tags are generated from the major/minor version in `pyproject.toml`
and the GitHub Actions run number. For version `0.1.0` on run `123`, CI
publishes the build tag `0.1.123-ALPHA`. It also updates the moving `alpha` tag
for installers and publishes `latest` on the default branch. Release tag pushes
must match the project version, such as `v0.1.0` or `v0.1.0-ALPHA`.

Configure these repository settings to enable Docker Hub publishing on
default-branch and tag builds:

- Secret `DOCKERHUB_USERNAME`: Docker Hub username.
- Secret `DOCKERHUB_TOKEN`: Docker Hub access token.
- Variable `DOCKERHUB_REPOSITORY`: optional image repository such as
	`yourname/avarch`; defaults to `asmir100/avarch`.

Equivalent direct commands:

```sh
uv run pytest
uv run ruff check .
uv run ruff format .
uv run pyright
```

Docker and wrapper development commands:

```sh
make docker-build
make wrapper-init
make wrapper-doctor
```

Cleanup commands:

```sh
make clean-preview
make clean-cache
make clean
make clean-env
```

Development expectations:

- Add or update focused tests for behavior changes.
- Run `make check` before submitting changes when the full toolchain is
	available.
- Use sample media or generated media fixtures for integration paths that need
	FFmpeg, FFprobe, VapourSynth, vspipe, Av1an, or SVT-AV1.
- Include command output, `avarch doctor`, relevant job IDs, plan IDs, logs, and
	workspace configuration details when reporting bugs.

## Advanced: Running Avarch Directly With Docker

The installed `avarch` command is the supported user interface. Direct Docker
invocation is mainly useful for debugging, CI, and custom environments.

The wrapper uses the current workspace mounted at `/workspace`, sets the
container working directory to `/workspace`, and maps the host user and group:

```sh
docker run --rm \
	--user "$(id -u):$(id -g)" \
	-v "$PWD:/workspace" \
	-w /workspace \
	avarch:latest \
	avarch --version
```

For workspace commands, run from the workspace root or mount the discovered
workspace root:

```sh
docker run --rm \
	--user "$(id -u):$(id -g)" \
	-v "$PWD:/workspace" \
	-w /workspace \
	avarch:latest \
	scan .
```

For a detached scheduler container, mirror the wrapper's scheduler naming and
labels:

```sh
docker run -d --rm \
	--name avarch-encoder \
	--label org.avarch.container=encoder \
	--label "org.avarch.workspace=$PWD" \
	--user "$(id -u):$(id -g)" \
	-v "$PWD:/workspace" \
	-w /workspace \
	avarch:latest \
	scheduler run
```

Wrapper environment variables:

- `AVARCH_IMAGE`: image reference, default `avarch:latest`.
- `AVARCH_DEFAULT_IMAGE`: fallback image reference used by installed wrappers
	when `AVARCH_IMAGE` is not set.
- `AVARCH_DOCKER_SECURITY_OPT`: override Docker `--security-opt`; on SELinux
	hosts the wrapper defaults to `label=disable` when needed.
- `AVARCH_DOCKER_VOLUME_OPTIONS`: mount options appended to the workspace volume.

GPU options are not implemented or documented by the wrapper.

## Advanced: Building Avarch

Build the local runtime image:

```sh
docker build -t avarch:latest .
```

Use the local image through the wrapper:

```sh
AVARCH_IMAGE=avarch:latest ./bin/avarch --version
```

Create a disposable workspace and run a smoke test:

```sh
tmpdir=$(mktemp -d)
cd "$tmpdir"
AVARCH_IMAGE=avarch:latest /path/to/avarch/bin/avarch init
AVARCH_IMAGE=avarch:latest /path/to/avarch/bin/avarch doctor
```

The smoke test verifies workspace initialization, database setup, config parsing,
and detected runtime identity. It does not encode media unless you add media and
run the Quick Start workflow.

## Advanced: Running Outside Docker

Native execution requires users to provide the full media toolchain. `uv sync`
installs the Python project dependencies; it does not install FFmpeg, FFprobe,
Av1an, VapourSynth system components, vspipe, SVT-AV1, or native plugins for
you.

Native requirements:

- Python `>=3.12`.
- `uv`.
- FFmpeg and FFprobe.
- Av1an `0.5.x`.
- VapourSynth and `vspipe`.
- BestSource support or another script-compatible source path.
- SVT-AV1 encoder available as expected by Av1an.
- Any plugins required by your custom `.vpy` templates or filter scripts.

Development startup:

```sh
uv sync
uv run avarch --version
uv run avarch init
uv run avarch doctor
```

Native limitations are the same product limitations as the container path, plus
any differences caused by locally installed media-tool versions and plugins.

## Architecture

The adopted architectural contract for new code and the planned structural refactor lives in
[docs/architecture.md](docs/architecture.md).

```text
Media files
		|
		v
Local .avarch workspace
		|
		v
Scan and probe
		|
		v
Profile + VapourSynth template
		|
		v
Resolved plan
		|
		v
Av1an video encode
		|
		v
FFmpeg mux
		|
		v
Validation
		|
		v
Promotion
```

Deep internals should live under `docs/` as the project grows.

## License

No `LICENSE` file is currently present in this repository. Add a license file
before publishing or redistributing Avarch outside its current development
context.
