#!/usr/bin/env sh
set -eu

image="${AVARCH_PROGRESS_IMAGE:-avarch-progress-fixtures}"
fixture_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
work_dir="$(mktemp -d)"

cleanup() {
    docker run --rm -v "$work_dir:/work" --entrypoint sh "$image" -c 'chmod -R a+rwX /work' >/dev/null 2>&1 || true
    rm -rf "$work_dir"
}
trap cleanup EXIT

ffmpeg -hide_banner -loglevel error \
    -f lavfi -i testsrc2=size=160x90:rate=24:duration=5 \
    -an -pix_fmt yuv420p "$work_dir/source.mkv"

common_args='-i /work/source.mkv --encoder svt-av1 --workers 1 --pix-format yuv420p --concat ffmpeg --max-tries 1 --audio-params -an --no-defaults -n'

docker run --rm -v "$work_dir:/work" --entrypoint av1an "$image" \
    -i /work/source.mkv \
    -o /work/out.ivf \
    --temp /work/av1an-temp \
    --encoder svt-av1 \
    --video-params "--preset 10 --crf 55" \
    --workers 1 \
    --pix-format yuv420p \
    --concat ffmpeg \
    --max-tries 1 \
    --audio-params -an \
    --no-defaults \
    -n \
    >"$fixture_dir/encode_non_tty_stdout.txt" \
    2>"$fixture_dir/encode_non_tty_stderr.txt"

set +e
docker run --rm -v "$work_dir:/work" --entrypoint av1an "$image" \
    -i /work/source.mkv \
    -o /work/out-failure.ivf \
    --temp /work/av1an-temp-failure \
    --encoder svt-av1 \
    --video-params "--definitely-not-an-svt-option" \
    --workers 1 \
    --pix-format yuv420p \
    --concat ffmpeg \
    --max-tries 1 \
    --audio-params -an \
    --no-defaults \
    -n \
    >/dev/null \
    2>"$fixture_dir/encode_failure_stderr.txt"
failure_code="$?"
set -e
printf 'failure_code=%s\n' "$failure_code" >"$fixture_dir/encode_failure_exit.txt"

script -q -e -c "docker run --rm -t -v '$work_dir:/work' --entrypoint av1an '$image' $common_args -o /work/out-tty.ivf --temp /work/av1an-temp-tty --video-params '--preset 10 --crf 55'" /dev/null \
    >"$fixture_dir/encode_tty_raw.bin"
