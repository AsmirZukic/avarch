# avarch

Av1an-first archival transcoding orchestration.

## Fresh Machine Setup

From a checked-out copy of this repo:

```sh
./scripts/bootstrap
```

That one command:

- syncs the Python environment from `uv.lock`
- creates `avarch.toml` if it is missing
- creates the local SQLite database in `.avarch/`
- runs `avarch doctor`
- reports whether the external media tools are available on `PATH`
- exposes the `uv`-managed VapourSynth tools, including `vspipe`
- installs Av1an `0.5.x` with Cargo if Av1an is missing or incompatible

If `uv` is missing, install it first:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new shell after installing `uv`, then rerun `./scripts/bootstrap`.

## Development Schema Reset

avarch is pre-release and currently provides no development-schema upgrade
compatibility.

After schema-reset changes, remove the existing `.avarch` directory and rebuild
local state. The source media library is not modified by this reset.

```sh
rm -rf .avarch

uv run avarch init --config ./avarch.toml
uv run avarch db upgrade --config ./avarch.toml
uv run avarch scan --config ./avarch.toml
```

## Day-to-Day Commands

```sh
make bootstrap      # full local setup/check
make run            # open the TUI
make doctor         # verify config and database
make test           # run tests
make check          # lint, typecheck, and test
```

Equivalent direct commands:

```sh
uv run avarch tui
uv run avarch doctor
uv run avarch scan /path/to/media
uv run avarch files
```

## Media Toolchain

The Python app can start after `./scripts/bootstrap`. Probing and encoding also
need these executables on `PATH`:

- `ffprobe` for source metadata
- `ffmpeg` for final muxing
- `vspipe` for VapourSynth runtime checks and script execution
- `av1an` for AV1 encoding

The current execution contract expects Av1an `0.5.x`.

Bootstrap intentionally stays in the `uv` and Cargo lane. `uv sync` installs
the Python/VapourSynth packages and exposes `vspipe` from `.venv/bin`. If
VapourSynth's normal config step cannot find the active Python shared library,
bootstrap writes the same per-user VapourSynth config from the venv metadata.

Av1an is installed with Cargo. When Av1an's build expects linker names such as
`libvapoursynth.so` and `libvapoursynth-script.so`, bootstrap creates local
build-only symlinks under `.avarch/bootstrap/lib` that point at the
uv-managed VapourSynth libraries. It does not require manual symlinks in
`/usr/lib`.

`ffmpeg` and `ffprobe` are still system tools. Bootstrap reports them when they
are missing, but does not install OS packages.

To change the compatible Av1an range later:

```sh
AVARCH_AV1AN_VERSION_REQ='>=0.5,<0.6' ./scripts/bootstrap
```

## Basic Workflow

```sh
uv run avarch scan /path/to/media
uv run avarch probe /path/to/media/movie.mkv
uv run avarch inspect /path/to/media/movie.mkv
uv run avarch plan /path/to/media/movie.mkv --profile av1_1080p_sdr
uv run avarch encode /path/to/media/movie.mkv --profile av1_1080p_sdr --dry-run
```

Remove `--dry-run` from `encode` when the generated command and artifacts look
right.

## Configuration

The default config lives at `avarch.toml`. If it does not exist, normal `avarch`
commands create it automatically with defaults.

To use a different config path:

```sh
AVARCH_CONFIG=/path/to/avarch.toml ./scripts/bootstrap
uv run avarch doctor --config /path/to/avarch.toml
```
