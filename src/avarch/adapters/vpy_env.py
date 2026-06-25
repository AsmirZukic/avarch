from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from avarch.application.planning import PlanningRuntimeIdentity
from avarch.serialization import canonical_json
from avarch.workspace import WorkspaceContext, WorkspaceError, ensure_workspace_layout

VPY_REQUIREMENTS_SCHEMA_VERSION = 1
VPY_LOCK_SCHEMA_VERSION = 1
ENVIRONMENT_ID_PREFIX_VERSION = "vpy-env-v1"


class VpyEnvironmentError(RuntimeError):
    pass


class PythonRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packages: list[str] = Field(default_factory=list)

    @field_validator("packages")
    @classmethod
    def normalize_packages(cls, value: list[str]) -> list[str]:
        return _dedupe_preserving_order(_normalize_nonempty_strings(value))


class VsrepoRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packages: list[str] = Field(default_factory=list)

    @field_validator("packages")
    @classmethod
    def normalize_packages(cls, value: list[str]) -> list[str]:
        return _dedupe_preserving_order(_normalize_nonempty_strings(value))


class AssetRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(default_factory=list)

    @field_validator("paths")
    @classmethod
    def normalize_paths(cls, value: list[str]) -> list[str]:
        return _dedupe_preserving_order(_normalize_nonempty_strings(value))


class VpyRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = VPY_REQUIREMENTS_SCHEMA_VERSION
    python: PythonRequirements = Field(default_factory=PythonRequirements)
    vsrepo: VsrepoRequirements = Field(default_factory=VsrepoRequirements)
    assets: AssetRequirements = Field(default_factory=AssetRequirements)


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    manifest_hash: str
    avarch_image_digest: str
    python_version: str
    python_abi: str
    platform: str
    vapoursynth_version: str
    environment_id: str


@dataclass(frozen=True, slots=True)
class VpyEnvironment:
    workspace: WorkspaceContext
    requirements: VpyRequirements
    identity: RuntimeIdentity
    path: Path
    lock_path: Path


def load_requirements(workspace: WorkspaceContext) -> VpyRequirements:
    ensure_workspace_layout(workspace)
    try:
        with workspace.vpy_requirements_toml.open("rb") as requirements_file:
            data = tomllib.load(requirements_file)
    except tomllib.TOMLDecodeError as exc:
        raise VpyEnvironmentError(f"Invalid VapourSynth requirements manifest: {exc}") from exc
    return VpyRequirements.model_validate(data)


def write_requirements(workspace: WorkspaceContext, requirements: VpyRequirements) -> None:
    ensure_workspace_layout(workspace)
    workspace.vpy_requirements_toml.write_text(
        render_requirements(requirements),
        encoding="utf-8",
    )


def render_requirements(requirements: VpyRequirements) -> str:
    return "\n".join(
        (
            f"schema_version = {requirements.schema_version}",
            "",
            "[python]",
            f"packages = {_toml_string_list(requirements.python.packages)}",
            "",
            "[vsrepo]",
            f"packages = {_toml_string_list(requirements.vsrepo.packages)}",
            "",
            "[assets]",
            f"paths = {_toml_string_list(requirements.assets.paths)}",
            "",
        )
    )


def add_python_package(workspace: WorkspaceContext, package: str) -> VpyRequirements:
    requirements = load_requirements(workspace)
    packages = _dedupe_preserving_order(
        [*requirements.python.packages, _normalize_package(package)]
    )
    updated = requirements.model_copy(
        update={"python": requirements.python.model_copy(update={"packages": packages})}
    )
    write_requirements(workspace, updated)
    return updated


def add_vsrepo_package(workspace: WorkspaceContext, package: str) -> VpyRequirements:
    requirements = load_requirements(workspace)
    packages = _dedupe_preserving_order(
        [*requirements.vsrepo.packages, _normalize_package(package)]
    )
    updated = requirements.model_copy(
        update={"vsrepo": requirements.vsrepo.model_copy(update={"packages": packages})}
    )
    write_requirements(workspace, updated)
    return updated


def remove_python_package(workspace: WorkspaceContext, package: str) -> VpyRequirements:
    package = _normalize_package(package)
    requirements = load_requirements(workspace)
    packages = [item for item in requirements.python.packages if item != package]
    updated = requirements.model_copy(
        update={"python": requirements.python.model_copy(update={"packages": packages})}
    )
    write_requirements(workspace, updated)
    return updated


def remove_vsrepo_package(workspace: WorkspaceContext, package: str) -> VpyRequirements:
    package = _normalize_package(package)
    requirements = load_requirements(workspace)
    packages = [item for item in requirements.vsrepo.packages if item != package]
    updated = requirements.model_copy(
        update={"vsrepo": requirements.vsrepo.model_copy(update={"packages": packages})}
    )
    write_requirements(workspace, updated)
    return updated


def build_runtime_identity(requirements: VpyRequirements) -> RuntimeIdentity:
    manifest_hash = build_manifest_hash(requirements)
    python_version = platform.python_version()
    python_abi = sysconfig.get_config_var("SOABI") or (
        f"python{sys.version_info.major}{sys.version_info.minor}"
    )
    runtime_platform = f"{sys.platform}-{platform.machine().lower() or 'unknown'}"
    vapoursynth_version = detect_vapoursynth_version()
    image_digest = os.environ.get("AVARCH_IMAGE_DIGEST", "unknown")
    identity_payload = {
        "avarch_image_digest": image_digest,
        "manifest_hash": manifest_hash,
        "platform": runtime_platform,
        "python_abi": python_abi,
        "python_version": python_version,
        "vapoursynth_version": vapoursynth_version,
    }
    identity_hash = hashlib.blake2b(
        f"{ENVIRONMENT_ID_PREFIX_VERSION}\0".encode()
        + canonical_json(identity_payload).encode("utf-8"),
        digest_size=4,
    ).hexdigest()
    environment_id = (
        f"{runtime_platform}-python{sys.version_info.major}{sys.version_info.minor}"
        f"-vs{vapoursynth_version}-{identity_hash}"
    )
    return RuntimeIdentity(
        manifest_hash=manifest_hash,
        avarch_image_digest=image_digest,
        python_version=python_version,
        python_abi=python_abi,
        platform=runtime_platform,
        vapoursynth_version=vapoursynth_version,
        environment_id=environment_id,
    )


def planning_runtime_identity_for_data_dir(data_dir: Path) -> PlanningRuntimeIdentity:
    identity = build_runtime_identity(_vpy_requirements_for_data_dir(data_dir))
    return PlanningRuntimeIdentity(
        manifest_hash=identity.manifest_hash,
        avarch_image_digest=identity.avarch_image_digest,
        python_version=identity.python_version,
        python_abi=identity.python_abi,
        platform=identity.platform,
        vapoursynth_version=identity.vapoursynth_version,
        environment_id=identity.environment_id,
    )


def build_manifest_hash(requirements: VpyRequirements) -> str:
    payload = requirements.model_dump(mode="json")
    return hashlib.blake2b(
        b"vpy-requirements-v1\0" + canonical_json(payload).encode("utf-8"),
        digest_size=16,
    ).hexdigest()


def detect_vapoursynth_version() -> str:
    try:
        import vapoursynth as vs  # type: ignore[import-not-found]
    except ImportError:
        return "unavailable"

    version = getattr(vs, "__version__", None)
    if version is not None:
        return str(version)
    api_version = getattr(vs, "API_VERSION", None)
    if api_version is not None:
        return str(api_version)
    return "unknown"


def _vpy_requirements_for_data_dir(data_dir: Path) -> VpyRequirements:
    workspace = _workspace_for_data_dir(data_dir)
    if workspace is None or not workspace.vpy_requirements_toml.exists():
        return VpyRequirements()
    return load_requirements(workspace)


def _workspace_for_data_dir(data_dir: Path) -> WorkspaceContext | None:
    resolved = data_dir.resolve()
    if resolved.name == "data" and resolved.parent.name == ".avarch":
        return WorkspaceContext(resolved.parent.parent)
    if resolved.name == ".avarch":
        return WorkspaceContext(resolved.parent)
    try:
        return WorkspaceContext.discover(resolved)
    except WorkspaceError:
        return None


def sync_environment(workspace: WorkspaceContext) -> VpyEnvironment:
    requirements = load_requirements(workspace)
    identity = build_runtime_identity(requirements)
    environment_dir = workspace.vpy_environments_dir / identity.environment_id
    lock_path = environment_dir / "lock.toml"

    if lock_path.exists():
        _write_current_environment(workspace, identity.environment_id)
        return VpyEnvironment(workspace, requirements, identity, environment_dir, lock_path)

    parent = workspace.vpy_environments_dir
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{identity.environment_id}.staging-",
            dir=parent,
        )
    )
    try:
        for name in ("python", "plugins", "libraries", "assets"):
            (staging / name).mkdir(parents=True, exist_ok=True)
        if requirements.python.packages:
            _install_python_packages(requirements.python.packages, staging / "python")
        (staging / "lock.toml").write_text(
            render_lock(requirements=requirements, identity=identity),
            encoding="utf-8",
        )
        os.replace(staging, environment_dir)
        _write_current_environment(workspace, identity.environment_id)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return VpyEnvironment(workspace, requirements, identity, environment_dir, lock_path)


def active_environment(workspace: WorkspaceContext) -> VpyEnvironment | None:
    ensure_workspace_layout(workspace)
    if not workspace.vpy_current.exists():
        return None
    environment_id = workspace.vpy_current.read_text(encoding="utf-8").strip()
    if not environment_id:
        return None
    requirements = load_requirements(workspace)
    identity = build_runtime_identity(requirements)
    environment_dir = workspace.vpy_environments_dir / environment_id
    lock_path = environment_dir / "lock.toml"
    if not lock_path.exists():
        return None
    return VpyEnvironment(workspace, requirements, identity, environment_dir, lock_path)


def runtime_environment_variables(workspace: WorkspaceContext) -> dict[str, str]:
    environment = active_environment(workspace)
    if environment is None:
        environment = sync_environment(workspace)

    python_paths = [
        str(workspace.scripts_dir),
        str(environment.path / "python"),
    ]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    if existing_pythonpath:
        python_paths.append(existing_pythonpath)

    library_paths = [str(environment.path / "libraries")]
    existing_library_path = os.environ.get("LD_LIBRARY_PATH")
    if existing_library_path:
        library_paths.append(existing_library_path)

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    env["VAPOURSYNTH_EXTRA_PLUGIN_PATH"] = str(environment.path / "plugins")
    env["LD_LIBRARY_PATH"] = os.pathsep.join(library_paths)
    return env


def render_lock(
    *,
    requirements: VpyRequirements,
    identity: RuntimeIdentity,
) -> str:
    lines = [
        f"schema_version = {VPY_LOCK_SCHEMA_VERSION}",
        f'environment_id = "{identity.environment_id}"',
        f'avarch_image_digest = "{_toml_escape(identity.avarch_image_digest)}"',
        f'manifest_hash = "{identity.manifest_hash}"',
        f'python_version = "{identity.python_version}"',
        f'python_abi = "{_toml_escape(identity.python_abi)}"',
        f'platform = "{_toml_escape(identity.platform)}"',
        f'vapoursynth_version = "{_toml_escape(identity.vapoursynth_version)}"',
        "",
    ]
    for package in requirements.python.packages:
        lines.extend(
            (
                "[[packages]]",
                f'name = "{_toml_escape(package)}"',
                'kind = "python"',
                "",
            )
        )
    for package in requirements.vsrepo.packages:
        lines.extend(
            (
                "[[packages]]",
                f'name = "{_toml_escape(package)}"',
                'kind = "native"',
                "",
            )
        )
    return "\n".join(lines)


def _install_python_packages(packages: list[str], target: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--target",
        str(target),
        *packages,
    ]
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise VpyEnvironmentError("Unable to install workspace Python dependencies.") from exc


def _write_current_environment(workspace: WorkspaceContext, environment_id: str) -> None:
    workspace.vpy_current.write_text(f"{environment_id}\n", encoding="utf-8")


def _normalize_nonempty_strings(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            raise ValueError("values must not be empty")
        normalized.append(text)
    return normalized


def _normalize_package(package: str) -> str:
    package = package.strip()
    if not package:
        raise VpyEnvironmentError("Package name must not be empty.")
    return package


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _toml_string_list(values: list[str]) -> str:
    return "[" + ", ".join(f'"{_toml_escape(value)}"' for value in values) + "]"


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
