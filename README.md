# avarch

Av1an-first archival transcoding orchestration.

## Docker Setup

From a checked-out copy of this repo:

```sh
docker build -t avarch:latest .
```

Install or invoke the host wrapper from `bin/avarch`. The wrapper discovers the
nearest `.avarch` workspace, mounts it at `/workspace`, runs the container as the
invoking UID/GID, preserves Docker exit codes, and uses the fixed
`avarch-encoder` container name for `scheduler run`.

```sh
export PATH="$PWD/bin:$PATH"
```

On SELinux hosts, the wrapper runs the container with
`--security-opt label=disable` so Docker can read home-directory bind mounts
without recursively relabeling large media workspaces. Set
`AVARCH_DOCKER_SECURITY_OPT` to override that option, or to an empty value to
disable it. Set `AVARCH_DOCKER_VOLUME_OPTIONS=z` only if you explicitly want
Docker to relabel the workspace mount.

Initialize local workspace state:

```sh
avarch init
avarch doctor
```

The current product supports only the workspace layout created by `avarch init`.
Root-level `avarch.toml`, root-level `profiles/`, and `.avarch/avarch.db` are
not supported state.

Set `AVARCH_IMAGE` to use a non-default tag.

## Development Schema Reset

avarch is pre-release and currently provides no development-schema upgrade
compatibility.

After schema-reset changes, remove the existing `.avarch` directory and rebuild
local state. The source media library is not modified by this reset.

```sh
rm -rf .avarch

avarch init
avarch db upgrade
avarch scan .
```

## Day-to-Day Commands

```sh
make docker-build   # build the runtime image
make wrapper-init   # create .avarch/ through the Docker wrapper
make wrapper-doctor # verify workspace config and database through the wrapper
make doctor         # verify config and database
make scan           # scan configured media roots
make test           # run tests
make check          # lint, typecheck, and test
make clean          # restore the repo to a clean state
make clean-cache    # remove generated caches and build outputs only
make clean-all      # alias for make clean
make clean-env      # remove local Python environment/cache
```

## Media Toolchain

The Docker image includes the runtime tools used by probing, planning checks,
and encoding:

- `ffprobe` from Debian FFmpeg packages for source metadata
- `ffmpeg` from Debian FFmpeg packages for final muxing and broad common codec support
- `vspipe` from the Python/VapourSynth environment for runtime checks and script execution
- `av1an` built with Cargo for AV1 encoding

The current execution contract expects Av1an `0.5.x`.

## Basic Workflow

```sh
avarch scan .
avarch probe
avarch plan --profile av1_1080p_sdr
avarch enqueue
avarch scheduler run -d
avarch scheduler status
avarch jobs list
```

Files enter the workspace through `scan`. After that, pipeline commands consume
persisted workspace state by default: `probe` selects active scanned files with
missing or stale probes, `plan` selects current successful probes, and `enqueue`
selects current valid plans.

Target a specific file when needed:

```sh
avarch probe --file Movies/Test.mkv --force
avarch plan --profile av1_1080p_sdr --file Movies/Test.mkv
avarch enqueue --file Movies/Test.mkv
```

Inspect persisted resources:

```sh
avarch files list
avarch files show --file Movies/Test.mkv
avarch plans list
avarch plans show PLAN_ID
avarch jobs show JOB_ID
avarch jobs validate JOB_ID
```

Scheduler lifecycle commands manage the workspace-local scheduler process:

```sh
avarch scheduler pause
avarch scheduler resume
avarch scheduler stop
avarch scheduler restart
```

Detached scheduler state and logs live under `.avarch/run/` and `.avarch/logs/`.
Application-level detachment is for normal host processes. When running avarch
through a one-shot Docker command, detach the container itself with `docker run
-d` instead.

## Configuration

Workspace configuration lives at `.avarch/config.toml`. Media paths persisted in
the database are relative to the workspace so the workspace can be moved without
rewriting inventory records.

To reset a development checkout to the current product layout:

```sh
make clean-all
avarch init
```
