# avarch

Av1an-first archival transcoding orchestration.

## Docker Setup

From a checked-out copy of this repo:

```sh
docker build -t avarch .
```

Initialize local state in the mounted working directory:

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" avarch init --config /work/avarch.toml
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" avarch doctor --config /work/avarch.toml
```

Mount media at a stable container path, then use that path in avarch commands.
Keeping the container path stable keeps paths stored in SQLite portable between
runs.

```sh
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$PWD:/work" \
  -v /host/media:/media:ro \
  avarch scan /media --config /work/avarch.toml
```

## Development Schema Reset

avarch is pre-release and currently provides no development-schema upgrade
compatibility.

After schema-reset changes, remove the existing `.avarch` directory and rebuild
local state. The source media library is not modified by this reset.

```sh
rm -rf .avarch

docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" avarch init --config /work/avarch.toml
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" avarch db upgrade --config /work/avarch.toml
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -v /host/media:/media:ro \
  avarch scan /media --config /work/avarch.toml
```

## Day-to-Day Commands

```sh
make docker-build   # build the runtime image
make docker-init    # create avarch.toml and .avarch/ through Docker
make docker-doctor  # verify mounted config and database through Docker
make doctor         # verify config and database
make scan           # scan configured media roots
make test           # run tests
make check          # lint, typecheck, and test
```

Equivalent direct commands:

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" avarch doctor --config /work/avarch.toml
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -v /host/media:/media:ro \
  avarch scan /media --config /work/avarch.toml
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" avarch files --config /work/avarch.toml
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
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -v /host/media:/media:ro \
  avarch scan /media --config /work/avarch.toml
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -v /host/media:/media:ro \
  avarch probe /media/movie.mkv --config /work/avarch.toml
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -v /host/media:/media:ro \
  avarch inspect /media/movie.mkv --config /work/avarch.toml
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -v /host/media:/media:ro \
  avarch plan /media/movie.mkv --profile av1_1080p_sdr --config /work/avarch.toml
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -v /host/media:/media:ro \
  avarch encode /media/movie.mkv --profile av1_1080p_sdr --dry-run \
  --config /work/avarch.toml
```

Remove `--dry-run` from `encode` when the generated command and artifacts look
right.

## Configuration

The default config lives at `avarch.toml`. If it does not exist, normal `avarch`
commands create it automatically with defaults.

To use a different config path:

```sh
docker run --rm --user "$(id -u):$(id -g)" -v /path/to/state:/work avarch doctor --config /work/avarch.toml
```
