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
    LIBRARY_PATH="/opt/avarch/lib" \
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

RUN mkdir -p "${AVARCH_LIB_DIR}" \
    && python - <<'PY'
from __future__ import annotations

import os
from pathlib import Path

import vapoursynth
from vapoursynth._utils import get_vsscript


lib_dir = Path(os.environ["AVARCH_LIB_DIR"])
lib_dir.mkdir(parents=True, exist_ok=True)


def force_symlink(link: Path, target: Path) -> None:
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target)


package_dir = Path(vapoursynth.__file__).parent
vapoursynth_libs = sorted(package_dir.glob("libvapoursynth.so*"))
if vapoursynth_libs:
    target = vapoursynth_libs[-1].resolve()
    force_symlink(lib_dir / "libvapoursynth.so", target)
    force_symlink(lib_dir / target.name, target)

vsscript = Path(get_vsscript()).resolve()
if vsscript.is_file():
    force_symlink(lib_dir / "libvapoursynth-script.so", vsscript)
    force_symlink(lib_dir / "libvsscript.so", vsscript)
    force_symlink(lib_dir / vsscript.name, vsscript)
PY

RUN vapoursynth config >/dev/null 2>&1 || true \
    && python - <<'PY'
from __future__ import annotations

import json
import os
import sys
import sysconfig
import tomllib
from pathlib import Path

from vapoursynth._utils import (
    _get_vapoursynth_config_path,
    _mangle_vsscript_key,
    get_vsscript,
)


def candidate_python_libraries() -> list[Path]:
    names: list[str] = []
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    for key in ("INSTSONAME", "LDLIBRARY", "LIBRARY"):
        value = sysconfig.get_config_var(key)
        if value and ".so" in value:
            names.append(value)
    names.extend(
        [
            f"libpython{version}.so",
            f"libpython{version}.so.1.0",
            "libpython3.so",
        ]
    )

    dirs: list[Path] = []
    for key in ("LIBDIR", "LIBPL", "srcdir"):
        value = sysconfig.get_config_var(key)
        if value:
            dirs.append(Path(value))
    for root in (Path(sys.base_prefix), Path(sys.prefix), Path(sys.exec_prefix)):
        dirs.extend([root / "lib", root / "lib64"])

    return [directory / name for directory in dirs for name in names]


python_library = next((path.resolve() for path in candidate_python_libraries() if path.is_file()), None)
if python_library is None:
    raise SystemExit("could not find a shared libpython for VapourSynth")

config_path = _get_vapoursynth_config_path()
config_path.parent.mkdir(parents=True, exist_ok=True)
try:
    contents = tomllib.loads(config_path.read_text(encoding="utf-8"))
except Exception:
    contents = {}

vsscript_paths = [Path(get_vsscript()).absolute()]
lib_dir = os.environ.get("AVARCH_LIB_DIR")
if lib_dir:
    lib_path = Path(lib_dir).absolute()
    vsscript_paths.extend(
        [
            lib_path / "libvsscript.so",
            lib_path / "libvapoursynth-script.so",
        ]
    )

for vsscript_path in vsscript_paths:
    if vsscript_path.exists():
        contents[_mangle_vsscript_key(str(vsscript_path))] = [
            sys.executable,
            str(python_library),
        ]

config_path.write_text(
    "".join(
        f"{json.dumps(key)} = [{json.dumps(value[0])},{json.dumps(value[1])}]\n"
        for key, value in contents.items()
    ),
    encoding="utf-8",
)
PY

RUN CARGO_REGISTRIES_CRATES_IO_PROTOCOL=git \
    cargo install av1an --version "${AVARCH_AV1AN_VERSION_REQ}" --locked \
    && rm -rf "${CARGO_HOME}/registry" "${CARGO_HOME}/git"

RUN avarch --version \
    && ffmpeg -version >/dev/null \
    && ffprobe -version >/dev/null \
    && vspipe --version >/dev/null \
    && av1an --version >/dev/null

WORKDIR /work

ENTRYPOINT ["avarch"]
