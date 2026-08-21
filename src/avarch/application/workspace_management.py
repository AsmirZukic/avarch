from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from avarch.config import WORKSPACE_CONFIG_TEXT, AppConfig, load_config, resolve_data_dir
from avarch.profiles.registry import ProfileRegistry, ProfileRegistryError


class WorkspaceManagementError(RuntimeError):
    pass


ExceptionTypes = tuple[type[Exception], ...]


@dataclass(frozen=True, slots=True)
class WorkspaceInitResult:
    workspace: Any
    config: AppConfig
    profile_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceInfo:
    root: Path
    config_toml: Path
    database_path: Path
    profiles_dir: Path
    scripts_dir: Path


@dataclass(frozen=True, slots=True)
class ConfigSummary:
    config_path: Path
    data_dir: Path
    database_url: str
    profile_search_paths: tuple[Path, ...]


def initialize_workspace(
    *,
    workspace_root: Path,
    force: bool,
    create_workspace_func: Callable[..., Any],
    workspace_errors: ExceptionTypes = (),
) -> WorkspaceInitResult:
    try:
        workspace = create_workspace_func(workspace_root, force=force)
    except workspace_errors as exc:
        raise WorkspaceManagementError(str(exc)) from exc

    workspace.config_toml.write_text(WORKSPACE_CONFIG_TEXT, encoding="utf-8")
    config = load_config(workspace.config_toml)
    try:
        profile_names = tuple(
            profile.name for profile in ProfileRegistry.from_config(config).list_profiles()
        )
    except ProfileRegistryError as exc:
        raise WorkspaceManagementError(str(exc)) from exc
    return WorkspaceInitResult(
        workspace=workspace,
        config=config,
        profile_names=profile_names,
    )


def discover_workspace_info(
    path: Path,
    *,
    discover_workspace_func: Callable[[Path], Any],
    workspace_errors: ExceptionTypes = (),
) -> WorkspaceInfo:
    try:
        workspace = discover_workspace_func(path)
    except workspace_errors as exc:
        raise WorkspaceManagementError(str(exc)) from exc
    return WorkspaceInfo(
        root=workspace.root,
        config_toml=workspace.config_toml,
        database_path=workspace.database_path,
        profiles_dir=workspace.profiles_dir,
        scripts_dir=workspace.scripts_dir,
    )


def summarize_config(
    *,
    config_path: Path,
    config: AppConfig,
    database_url: str,
) -> ConfigSummary:
    return ConfigSummary(
        config_path=config_path,
        data_dir=resolve_data_dir(config, config_path),
        database_url=database_url,
        profile_search_paths=tuple(config.profile_registry.search_paths),
    )
