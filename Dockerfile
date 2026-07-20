ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.10.7
ARG AVARCH_AV1AN_VERSION=0.5.1
ARG SVT_AV1_VERSION=2.3.0

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM rust:1.88-slim AS rust-toolchain

FROM python:${PYTHON_VERSION}-slim AS python-builder

ARG AVARCH_AV1AN_VERSION
ARG SVT_AV1_VERSION

ENV DEBIAN_FRONTEND=noninteractive \
    UV_PROJECT_ENVIRONMENT=/opt/avarch/venv \
    VIRTUAL_ENV=/opt/avarch/venv \
    CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    AVARCH_LIB_DIR=/opt/avarch/lib \
    XDG_CONFIG_HOME=/opt/avarch/config \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    CARGO_HTTP_CAINFO=/etc/ssl/certs/ca-certificates.crt \
    PATH="/opt/avarch/venv/bin:/usr/local/cargo/bin:${PATH}" \
    VS_SITE_PACKAGES=/opt/avarch/venv/lib/python3.12/site-packages/vapoursynth \
    LIBRARY_PATH=/opt/avarch/lib \
    LD_LIBRARY_PATH="/opt/avarch/lib:/opt/avarch/venv/lib/python3.12/site-packages/vapoursynth:/usr/local/lib:/usr/lib/x86_64-linux-gnu"

COPY --from=uv /uv /usr/local/bin/uv
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        clang \
        cmake \
        curl \
        ffmpeg \
        git \
        libclang-dev \
        libssl-dev \
        nasm \
        pkg-config \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "v${SVT_AV1_VERSION}" https://gitlab.com/AOMediaCodec/SVT-AV1.git /tmp/SVT-AV1 \
    && cmake -S /tmp/SVT-AV1 -B /tmp/SVT-AV1/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_DEC=OFF \
        -DBUILD_SHARED_LIBS=OFF \
    && cmake --build /tmp/SVT-AV1/build --target SvtAv1EncApp --parallel "$(nproc)" \
    && install -m 0755 /tmp/SVT-AV1/Bin/Release/SvtAv1EncApp /usr/local/bin/SvtAv1EncApp \
    && rm -rf /tmp/SVT-AV1

WORKDIR /app

COPY pyproject.toml uv.lock alembic.ini ./
COPY migrations ./migrations
COPY src ./src

RUN uv sync --locked --no-dev --no-editable

RUN uv pip install "git+https://github.com/vapoursynth/vsrepo.git"

RUN mkdir -p "${AVARCH_LIB_DIR}" \
    && python - <<'PY'
from __future__ import annotations

import os
from pathlib import Path

import vapoursynth
from vapoursynth._utils import get_vsscript


def force_symlink(link: Path, target: Path) -> None:
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target)


lib_dir = Path(os.environ["AVARCH_LIB_DIR"])
package_dir = Path(vapoursynth.__file__).parent

vapoursynth_lib = sorted(package_dir.glob("libvapoursynth.so*"))[-1].resolve()
force_symlink(lib_dir / "libvapoursynth.so", vapoursynth_lib)

vsscript_lib = Path(get_vsscript()).resolve()
force_symlink(lib_dir / "libvapoursynth-script.so", vsscript_lib)
PY

RUN python - <<'PY'
from __future__ import annotations

import json
import sys
import sysconfig
from pathlib import Path

from vapoursynth._utils import (
    _get_vapoursynth_config_path,
    _mangle_vsscript_key,
    get_vsscript,
)


libpython_name = sysconfig.get_config_var("INSTSONAME")
libpython_dir = sysconfig.get_config_var("LIBDIR")
if not libpython_name or not libpython_dir:
    raise SystemExit("could not resolve libpython from sysconfig")

python_library = Path(libpython_dir, libpython_name).resolve()
if not python_library.is_file():
    raise SystemExit("could not find a shared libpython for VapourSynth")

config_path = _get_vapoursynth_config_path()
config_path.parent.mkdir(parents=True, exist_ok=True)

vsscript = str(Path(get_vsscript()).resolve())
key = _mangle_vsscript_key(vsscript)
config_path.write_text(
    f"{json.dumps(key)} = [{json.dumps(sys.executable)},{json.dumps(str(python_library))}]\n",
    encoding="utf-8",
)
PY

RUN chmod -R a+rX "${XDG_CONFIG_HOME}"

RUN CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse \
    cargo install av1an --version "=${AVARCH_AV1AN_VERSION}" --locked \
    && rm -rf "${CARGO_HOME}/registry" "${CARGO_HOME}/git"

RUN avarch --version \
    && ffmpeg -version >/dev/null \
    && ffprobe -version >/dev/null \
    && vspipe --version >/dev/null \
    && SvtAv1EncApp --version >/dev/null \
    && av1an --version >/dev/null

FROM python:${PYTHON_VERSION}-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    VIRTUAL_ENV=/opt/avarch/venv \
    AVARCH_LIB_DIR=/opt/avarch/lib \
    AVARCH_MIGRATIONS_ROOT=/opt/avarch/app \
    XDG_CONFIG_HOME=/opt/avarch/config \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PATH="/opt/avarch/venv/bin:${PATH}" \
    VS_SITE_PACKAGES=/opt/avarch/venv/lib/python3.12/site-packages/vapoursynth \
    LD_LIBRARY_PATH="/opt/avarch/lib:/opt/avarch/venv/lib/python3.12/site-packages/vapoursynth:/usr/local/lib:/usr/lib/x86_64-linux-gnu"

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && apt-get purge -y --auto-remove perl \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-builder /opt/avarch/venv /opt/avarch/venv
COPY --from=python-builder /opt/avarch/lib /opt/avarch/lib
COPY --from=python-builder /opt/avarch/config /opt/avarch/config
COPY --from=python-builder /app/alembic.ini /opt/avarch/app/alembic.ini
COPY --from=python-builder /app/migrations /opt/avarch/app/migrations
COPY --from=python-builder /usr/local/bin/SvtAv1EncApp /usr/local/bin/SvtAv1EncApp
COPY --from=python-builder /usr/local/cargo/bin/av1an /usr/local/bin/av1an

RUN set -eu; \
    rm -rf \
        /usr/local/bin/pip \
        /usr/local/bin/pip3 \
        /usr/local/bin/pip3.* \
        /usr/local/lib/python*/ensurepip \
        /usr/local/lib/python*/site-packages/pip \
        /usr/local/lib/python*/site-packages/pip-* \
        /usr/local/lib/python*/site-packages/setuptools \
        /usr/local/lib/python*/site-packages/setuptools-* \
        /usr/local/lib/python*/site-packages/wheel \
        /usr/local/lib/python*/site-packages/wheel-* \
        /opt/avarch/venv/bin/pip \
        /opt/avarch/venv/bin/pip3 \
        /opt/avarch/venv/bin/pip3.* \
        /opt/avarch/venv/lib/python*/site-packages/pip \
        /opt/avarch/venv/lib/python*/site-packages/pip-* \
        /opt/avarch/venv/lib/python*/site-packages/setuptools \
        /opt/avarch/venv/lib/python*/site-packages/setuptools-* \
        /opt/avarch/venv/lib/python*/site-packages/wheel \
        /opt/avarch/venv/lib/python*/site-packages/wheel-*; \
    for package in perl 'perl-modules-*' libio-compress-perl libhttp-tiny-perl libsocket-perl; do \
        installed="$( \
            dpkg-query -W -f='${db:Status-Abbrev} ${binary:Package}\n' "$package" 2>/dev/null \
                | awk '$1 == "ii" { print $2 }' \
                || true \
        )"; \
        if [ -n "$installed" ]; then \
            printf 'Runtime image must not contain %s package(s):\n%s\n' "$package" "$installed" >&2; \
            exit 1; \
        fi; \
    done; \
    if python -m pip --version; then \
        echo "pip must not exist in the runtime image" >&2; \
        exit 1; \
    fi; \
    avarch --version; \
    ffmpeg -version >/dev/null; \
    ffprobe -version >/dev/null; \
    vspipe --version >/dev/null; \
    SvtAv1EncApp --version >/dev/null; \
    av1an --version >/dev/null; \
    python - <<'PY'
from __future__ import annotations

import pydantic_settings
import vapoursynth

print(pydantic_settings.__version__)
print(vapoursynth.__version__)
PY

WORKDIR /work

ENTRYPOINT ["avarch"]
