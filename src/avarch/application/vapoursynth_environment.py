from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class VapourSynthEnvironmentWorkflowError(RuntimeError):
    pass


class VapourSynthPackageSearchUnavailableError(VapourSynthEnvironmentWorkflowError):
    pass


ExceptionTypes = tuple[type[Exception], ...]


@dataclass(frozen=True, slots=True)
class VapourSynthEnvironmentSync:
    environment_id: str
    lock_path: Path


@dataclass(frozen=True, slots=True)
class VapourSynthEnvironmentInfo:
    requirements_path: Path
    environment_id: str
    manifest_hash: str
    avarch_image_digest: str
    python_version: str
    python_abi: str
    platform: str
    vapoursynth_version: str


@dataclass(frozen=True, slots=True)
class VapourSynthEnvironmentCheck:
    environment_id: str
    lock_path: Path
    plugin_namespaces: str


@dataclass(frozen=True, slots=True)
class VapourSynthPackageList:
    python_packages: tuple[str, ...]
    vsrepo_packages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VapourSynthPackageSearchResult:
    returncode: int
    stdout: str
    stderr: str
    matches: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class VapourSynthPluginItem:
    namespace: str
    name: str
    version: str | None
    source: str
    path: str | None


def sync_vapoursynth_environment(
    workspace: Any,
    *,
    sync_environment_func: Callable[[Any], Any],
    environment_errors: ExceptionTypes = (),
) -> VapourSynthEnvironmentSync:
    try:
        environment = sync_environment_func(workspace)
    except environment_errors as exc:
        raise VapourSynthEnvironmentWorkflowError(str(exc)) from exc
    return VapourSynthEnvironmentSync(
        environment_id=_environment_id(environment),
        lock_path=_lock_path(environment),
    )


def vapoursynth_environment_info(
    workspace: Any,
    *,
    load_requirements_func: Callable[[Any], Any],
    build_runtime_identity_func: Callable[[Any], Any],
    environment_errors: ExceptionTypes = (),
) -> VapourSynthEnvironmentInfo:
    try:
        requirements = load_requirements_func(workspace)
        identity = build_runtime_identity_func(requirements)
    except environment_errors as exc:
        raise VapourSynthEnvironmentWorkflowError(str(exc)) from exc
    return VapourSynthEnvironmentInfo(
        requirements_path=workspace.vpy_requirements_toml,
        environment_id=identity.environment_id,
        manifest_hash=identity.manifest_hash,
        avarch_image_digest=identity.avarch_image_digest,
        python_version=identity.python_version,
        python_abi=identity.python_abi,
        platform=identity.platform,
        vapoursynth_version=identity.vapoursynth_version,
    )


def check_vapoursynth_environment(
    workspace: Any,
    *,
    sync_environment_func: Callable[[Any], Any],
    list_plugins_func: Callable[[], Any],
    environment_errors: ExceptionTypes = (),
    plugin_inventory_errors: ExceptionTypes = (),
) -> VapourSynthEnvironmentCheck:
    synced = sync_vapoursynth_environment(
        workspace,
        sync_environment_func=sync_environment_func,
        environment_errors=environment_errors,
    )
    try:
        namespaces = ", ".join(plugin.namespace for plugin in list_plugins_func())
    except plugin_inventory_errors:
        namespaces = "unavailable"
    return VapourSynthEnvironmentCheck(
        environment_id=synced.environment_id,
        lock_path=synced.lock_path,
        plugin_namespaces=namespaces or "none",
    )


def list_vapoursynth_packages(
    workspace: Any,
    *,
    load_requirements_func: Callable[[Any], Any],
    environment_errors: ExceptionTypes = (),
) -> VapourSynthPackageList:
    try:
        requirements = load_requirements_func(workspace)
    except environment_errors as exc:
        raise VapourSynthEnvironmentWorkflowError(str(exc)) from exc
    return VapourSynthPackageList(
        python_packages=tuple(requirements.python.packages),
        vsrepo_packages=tuple(requirements.vsrepo.packages),
    )


def search_vapoursynth_packages(
    query: str,
    *,
    search_func: Callable[[str], Any],
    unavailable_errors: ExceptionTypes = (),
) -> VapourSynthPackageSearchResult:
    try:
        result = search_func(query)
    except unavailable_errors as exc:
        raise VapourSynthPackageSearchUnavailableError(str(exc)) from exc
    return VapourSynthPackageSearchResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        matches=result.matches,
    )


def install_vapoursynth_package(
    workspace: Any,
    *,
    name: str,
    kind: str,
    add_python_package_func: Callable[[Any, str], Any],
    add_vsrepo_package_func: Callable[[Any, str], Any],
    sync_environment_func: Callable[[Any], Any],
    environment_errors: ExceptionTypes = (),
) -> VapourSynthEnvironmentSync:
    try:
        if kind == "python":
            add_python_package_func(workspace, name)
        else:
            add_vsrepo_package_func(workspace, name)
        return sync_vapoursynth_environment(
            workspace,
            sync_environment_func=sync_environment_func,
            environment_errors=environment_errors,
        )
    except environment_errors as exc:
        raise VapourSynthEnvironmentWorkflowError(str(exc)) from exc


def remove_vapoursynth_package(
    workspace: Any,
    *,
    name: str,
    kind: str,
    remove_python_package_func: Callable[[Any, str], Any],
    remove_vsrepo_package_func: Callable[[Any, str], Any],
    sync_environment_func: Callable[[Any], Any],
    environment_errors: ExceptionTypes = (),
) -> VapourSynthEnvironmentSync:
    try:
        if kind == "python":
            remove_python_package_func(workspace, name)
        else:
            remove_vsrepo_package_func(workspace, name)
        return sync_vapoursynth_environment(
            workspace,
            sync_environment_func=sync_environment_func,
            environment_errors=environment_errors,
        )
    except environment_errors as exc:
        raise VapourSynthEnvironmentWorkflowError(str(exc)) from exc


def list_vapoursynth_plugin_items(
    *,
    list_plugins_func: Callable[[], Any],
    plugin_inventory_errors: ExceptionTypes = (),
) -> list[VapourSynthPluginItem]:
    try:
        plugins = list_plugins_func()
    except plugin_inventory_errors as exc:
        raise VapourSynthEnvironmentWorkflowError(str(exc)) from exc
    return [
        VapourSynthPluginItem(
            namespace=plugin.namespace,
            name=plugin.name,
            version=plugin.version,
            source=plugin.source,
            path=str(plugin.path) if plugin.path is not None else None,
        )
        for plugin in plugins
    ]


def _environment_id(environment: Any) -> str:
    identity = getattr(environment, "identity", None)
    environment_id = getattr(identity, "environment_id", None)
    if not isinstance(environment_id, str):
        raise VapourSynthEnvironmentWorkflowError("VapourSynth environment id is missing.")
    return environment_id


def _lock_path(environment: Any) -> Path:
    lock_path = getattr(environment, "lock_path", None)
    if not isinstance(lock_path, Path):
        raise VapourSynthEnvironmentWorkflowError("VapourSynth lock path is missing.")
    return lock_path
