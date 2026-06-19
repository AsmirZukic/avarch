FROM rust:1.88-slim AS rust-toolchain

FROM python:3.12-slim

ARG UV_VERSION=0.10.7
ARG AVARCH_AV1AN_VERSION_REQ=">=0.5,<0.6"

ENV DEBIAN_FRONTEND=noninteractive \
    UV_PROJECT_ENVIRONMENT=/opt/avarch/venv \
    VIRTUAL_ENV=/opt/avarch/venv \
    CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    AVARCH_LIB_DIR=/opt/avarch/lib \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    CARGO_HTTP_CAINFO=/etc/ssl/certs/ca-certificates.crt \
    PATH="/opt/avarch/venv/bin:/usr/local/cargo/bin:${PATH}" \
    VS_SITE_PACKAGES=/opt/avarch/venv/lib/python3.12/site-packages/vapoursynth \
    LIBRARY_PATH=/opt/avarch/lib \
    LD_LIBRARY_PATH="/opt/avarch/lib:/opt/avarch/venv/lib/python3.12/site-packages/vapoursynth:/usr/local/lib:/usr/lib/x86_64-linux-gnu"

COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup

RUN apt-get update \
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

RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app

COPY pyproject.toml uv.lock alembic.ini ./
COPY migrations ./migrations
COPY src ./src

RUN uv sync --locked --no-dev

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

RUN CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse \
    cargo install av1an --version "${AVARCH_AV1AN_VERSION_REQ}" --locked \
    && rm -rf "${CARGO_HOME}/registry" "${CARGO_HOME}/git"

RUN avarch --version \
    && ffmpeg -version >/dev/null \
    && ffprobe -version >/dev/null \
    && vspipe --version >/dev/null \
    && av1an --version >/dev/null

WORKDIR /work

ENTRYPOINT ["avarch"]
