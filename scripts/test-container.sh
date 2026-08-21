#!/usr/bin/env bash
set -euo pipefail

image="${1:-avarch:test}"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

docker run --rm "$image" --help >/dev/null
docker run --rm "$image" --version >/dev/null

docker run --rm --entrypoint av1an "$image" --version >/dev/null
docker run --rm --entrypoint ffmpeg "$image" -version >/dev/null
docker run --rm --entrypoint ffprobe "$image" -version >/dev/null
docker run --rm --entrypoint vspipe "$image" --version >/dev/null
docker run --rm --entrypoint SvtAv1EncApp "$image" --version >/dev/null

if docker run --rm --entrypoint python "$image" -m pip --version; then
    echo "pip must not exist in the runtime image" >&2
    exit 1
fi

docker run --rm --entrypoint sh "$image" -c '
set -eu
for package in perl "perl-modules-*" libio-compress-perl libhttp-tiny-perl libsocket-perl; do
    installed=$(dpkg-query -W -f="\${db:Status-Abbrev} \${binary:Package}\n" "$package" 2>/dev/null | awk "\$1 == \"ii\" { print \$2 }" || true)
    if [ -n "$installed" ]; then
        printf "Runtime image must not contain %s package(s):\n%s\n" "$package" "$installed" >&2
        exit 1
    fi
done
'

docker run --rm --entrypoint python "$image" -c 'import vapoursynth; print(vapoursynth.__version__)'

docker run --rm --entrypoint sh -v "$work_dir:/tmp/avarch-smoke" "$image" -c '
set -eu
cat > /tmp/avarch-smoke/blank.vpy <<"VPY"
import vapoursynth as vs
core = vs.core
clip = core.std.BlankClip(width=64, height=64, format=vs.YUV420P8, length=1, fpsnum=1, fpsden=1)
clip.set_output()
VPY
vspipe --info /tmp/avarch-smoke/blank.vpy - >/dev/null
ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc2=size=64x64:rate=1:duration=1 -pix_fmt yuv420p -an -y /tmp/avarch-smoke/source.y4m
SvtAv1EncApp -i /tmp/avarch-smoke/source.y4m -b /tmp/avarch-smoke/output.ivf --preset 12 --crf 63 >/dev/null
test -s /tmp/avarch-smoke/output.ivf
'
