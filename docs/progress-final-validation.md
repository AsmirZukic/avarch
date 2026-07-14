# Job Progress Observability Validation

Validated commit: `c23b7cc4c93738db4bd76336dab86e2a46278762`
Date: 2026-07-14
Environment: Linux workspace, Python 3.12.8, Docker runtime available

## Runtime

- Docker image: `avarch:progress-validation`
- Docker image id: `sha256:97aec3415bc394944484484884a3a84987e9579099f25d5727f960b9195de1a9`
- Avarch version: `0.1.0`
- Container Av1an version: `av1an 0.5.2-unstable`
- Host FFmpeg: `ffmpeg version N-121908-g7018ce14df`
- Container FFmpeg: `ffmpeg version 7.1.5-0+deb13u1`

## Commands Executed

```sh
uv run pytest
uv run ruff check .
uv run pyright
uv build
docker build -t avarch:progress-validation .
docker run --rm avarch:progress-validation --version
docker run --rm --entrypoint av1an avarch:progress-validation --version
docker run --rm --entrypoint ffmpeg avarch:progress-validation -version
scripts/test-container.sh avarch:progress-validation
```

## Results

- Automated suite: `884 passed, 4 skipped, 21 warnings`
- Lint: passed
- Type check: passed
- Package build: passed
- Docker build: passed
- Container runtime smoke: passed
- Container synthetic SVT-AV1 encode: passed
- Migration coverage: passed through `tests/test_migrations.py` in the full suite
- Progress parser, persistence, scheduler integration, CLI rendering, and watcher tests: passed in the full suite

## Notes

- Host `av1an --version` failed because `libvsscript.so` is not available on the host shell. The Docker image includes the supported Av1an 0.5.x runtime and passed the container Av1an version check.
- `uv run ruff format --check .` reports existing formatting drift in unrelated files. The configured repository validation uses `ruff check`, `pyright`, and `pytest`; those all passed. No broad formatter churn was applied.
- Cancellation, retry, failure, stale-heartbeat, legacy database/display, malformed telemetry, and bounded-write behavior are covered by automated tests rather than manual long-running media workflows in this validation run.
