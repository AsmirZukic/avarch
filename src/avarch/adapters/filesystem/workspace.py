from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_VPY_REQUIREMENTS_TEXT = """schema_version = 1

[python]
packages = []

[vsrepo]
packages = []

[assets]
paths = []
"""


class WorkspaceError(RuntimeError):
    pass


class WorkspaceAlreadyExistsError(WorkspaceError):
    pass


class WorkspaceNotFoundError(WorkspaceError):
    pass


class WorkspacePathError(WorkspaceError):
    pass


class WorkspaceCreateError(WorkspaceError):
    pass


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    root: Path

    @property
    def avarch_dir(self) -> Path:
        return self.root / ".avarch"

    @property
    def config_toml(self) -> Path:
        return self.avarch_dir / "config.toml"

    @property
    def profiles_dir(self) -> Path:
        return self.avarch_dir / "profiles"

    @property
    def scripts_dir(self) -> Path:
        return self.avarch_dir / "scripts"

    @property
    def vpy_dir(self) -> Path:
        return self.avarch_dir / "vpy"

    @property
    def vpy_requirements_toml(self) -> Path:
        return self.vpy_dir / "requirements.toml"

    @property
    def vpy_current(self) -> Path:
        return self.vpy_dir / "current"

    @property
    def vpy_environments_dir(self) -> Path:
        return self.vpy_dir / "environments"

    @property
    def data_dir(self) -> Path:
        return self.avarch_dir / "data"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "avarch.db"

    @classmethod
    def discover(cls, start: Path | None = None) -> WorkspaceContext:
        current = (start or Path.cwd()).resolve()
        if current.is_file():
            current = current.parent

        for candidate in (current, *current.parents):
            avarch_dir = candidate / ".avarch"
            if (avarch_dir / "config.toml").is_file():
                return cls(candidate)

        raise WorkspaceNotFoundError(
            "No Avarch workspace found. Run `avarch init` from the workspace root."
        )

    def resolve_inside(self, path: Path) -> Path:
        candidate = path if path.is_absolute() else self.root / path
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError(f"Path escapes the workspace: {path}") from exc
        return resolved

    def relative_path(self, path: Path) -> Path:
        resolved = self.resolve_inside(path)
        return resolved.relative_to(self.root)


def create_workspace(root: Path, *, force: bool = False) -> WorkspaceContext:
    root = root.resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise WorkspaceCreateError(_permission_message(root, exc)) from exc
    context = WorkspaceContext(root)

    if context.avarch_dir.exists():
        if not force:
            raise WorkspaceAlreadyExistsError(f"Workspace already exists: {context.avarch_dir}")
        try:
            shutil.rmtree(context.avarch_dir)
        except PermissionError as exc:
            raise WorkspaceCreateError(_permission_message(context.avarch_dir, exc)) from exc

    try:
        temp_dir = Path(
            tempfile.mkdtemp(
                prefix=".avarch.tmp-",
                dir=root,
            )
        )
    except PermissionError as exc:
        raise WorkspaceCreateError(_permission_message(root, exc)) from exc
    try:
        _populate_workspace_tree(temp_dir)
        os.replace(temp_dir, context.avarch_dir)
    except PermissionError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise WorkspaceCreateError(_permission_message(context.avarch_dir, exc)) from exc
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return context


def _permission_message(path: Path, exc: PermissionError) -> str:
    target = Path(exc.filename) if exc.filename else path
    return (
        f"Cannot initialize Avarch workspace at {path}: permission denied while writing "
        f"{target}. Check that the project directory is writable by the container user."
    )


def ensure_workspace_layout(context: WorkspaceContext) -> None:
    for directory in _workspace_directories(context.avarch_dir):
        directory.mkdir(parents=True, exist_ok=True)
    if not context.vpy_requirements_toml.exists():
        context.vpy_requirements_toml.write_text(DEFAULT_VPY_REQUIREMENTS_TEXT, encoding="utf-8")


def _populate_workspace_tree(avarch_dir: Path) -> None:
    for directory in _workspace_directories(avarch_dir):
        directory.mkdir(parents=True, exist_ok=True)

    (avarch_dir / "vpy" / "requirements.toml").write_text(
        DEFAULT_VPY_REQUIREMENTS_TEXT,
        encoding="utf-8",
    )


def _workspace_directories(avarch_dir: Path) -> tuple[Path, ...]:
    return (
        avarch_dir / "profiles",
        avarch_dir / "scripts",
        avarch_dir / "vpy",
        avarch_dir / "vpy" / "environments",
        avarch_dir / "data",
    )
