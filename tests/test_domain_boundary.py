from __future__ import annotations

import ast
from pathlib import Path

DOMAIN_ROOT = Path(__file__).resolve().parents[1] / "src" / "avarch" / "domain"
PACKAGE_ROOT = DOMAIN_ROOT.parent
APPLICATION_ROOT = PACKAGE_ROOT / "application"

FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "avarch.adapters",
        "avarch.application",
        "avarch.cli",
        "avarch.models",
        "pydantic",
        "sqlalchemy",
        "sqlmodel",
        "structlog",
        "subprocess",
        "typer",
    }
)


def test_domain_modules_do_not_import_forbidden_boundaries() -> None:
    violations: list[str] = []
    for path in _domain_module_paths():
        for imported_name in _imported_names(path):
            if _is_forbidden_import(imported_name):
                violations.append(f"{path.relative_to(DOMAIN_ROOT)} imports {imported_name}")

    assert violations == []


def test_domain_modules_only_import_domain_from_avarch() -> None:
    violations: list[str] = []
    for path in _domain_module_paths():
        for imported_name in _imported_names(path):
            if (
                imported_name == "avarch" or imported_name.startswith("avarch.")
            ) and not imported_name.startswith("avarch.domain"):
                violations.append(f"{path.relative_to(DOMAIN_ROOT)} imports {imported_name}")

    assert violations == []


def test_application_modules_do_not_import_concrete_boundaries() -> None:
    forbidden_roots = frozenset(
        {
            "avarch.adapters",
            "sqlmodel",
            "sqlalchemy",
            "subprocess",
            "typer",
        }
    )
    violations: list[str] = []
    for path in _application_module_paths():
        for imported_name in _imported_names(path):
            if any(
                imported_name == forbidden or imported_name.startswith(f"{forbidden}.")
                for forbidden in forbidden_roots
            ):
                violations.append(f"{path.relative_to(APPLICATION_ROOT)} imports {imported_name}")

    assert violations == []


def test_config_module_does_not_import_sqlalchemy() -> None:
    imported_sqlalchemy = sorted(
        imported_name
        for imported_name in _imported_names(PACKAGE_ROOT / "config.py")
        if imported_name == "sqlalchemy" or imported_name.startswith("sqlalchemy.")
    )

    assert imported_sqlalchemy == []


def test_cli_module_does_not_import_database_driver_boundaries() -> None:
    cli_path = PACKAGE_ROOT / "cli.py"
    forbidden_imports = {
        "avarch.adapters",
        "avarch.adapters.sqlite",
        "sqlalchemy",
        "sqlmodel",
    }
    forbidden_members = {
        ("avarch.bootstrap", "FfprobeCollector"),
        ("avarch.bootstrap", "ProbeError"),
        ("avarch.bootstrap", "VpyEnvironmentError"),
        ("avarch.bootstrap", "VpyPluginInventoryError"),
        ("avarch.bootstrap", "VsrepoUnavailableError"),
        ("avarch.bootstrap", "format_validation_report_summary"),
        ("avarch.bootstrap", "format_probe_summary"),
        ("avarch.bootstrap", "parse_normalized_probe_json"),
        ("avarch.bootstrap", "planning_runtime_identity_for_data_dir"),
        ("avarch.bootstrap", "runtime_environment_variables"),
    }
    violations = [
        f"cli.py imports {imported_name}"
        for imported_name in _imported_names(cli_path)
        if any(
            imported_name == forbidden or imported_name.startswith(f"{forbidden}.")
            for forbidden in forbidden_imports
        )
    ]
    violations.extend(
        f"cli.py imports {module_name}.{imported_member}"
        for module_name, imported_member in _imported_members(cli_path)
        if (module_name, imported_member) in forbidden_members
    )

    assert violations == []


def test_package_modules_do_not_form_import_cycles() -> None:
    modules = {_module_name_for_path(path) for path in _package_module_paths()}
    graph = {
        _module_name_for_path(path): _package_import_targets(path, modules)
        for path in _package_module_paths()
    }

    cycles = _import_cycles(graph)

    assert cycles == []


def test_job_state_fields_are_mutated_only_by_transition_adapter() -> None:
    allowed_paths = {
        PACKAGE_ROOT / "adapters" / "sqlite" / "job_transitions.py",
    }
    violations: list[str] = []
    for path in _package_module_paths():
        if path in allowed_paths:
            continue
        for line_number, field_name in _job_state_assignments(path):
            relative_path = path.relative_to(PACKAGE_ROOT.parent.parent)
            violations.append(f"{relative_path}:{line_number} assigns job.{field_name}")

    assert violations == []


def _domain_module_paths() -> list[Path]:
    return sorted(path for path in DOMAIN_ROOT.rglob("*.py") if path.is_file())


def _application_module_paths() -> list[Path]:
    if not APPLICATION_ROOT.exists():
        return []
    return sorted(path for path in APPLICATION_ROOT.rglob("*.py") if path.is_file())


def _package_module_paths() -> list[Path]:
    return sorted(path for path in PACKAGE_ROOT.rglob("*.py") if path.is_file())


def _module_name_for_path(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = relative.parts
    if parts == ("__init__",):
        return "avarch"
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(("avarch", *parts))


def _imported_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_names.append(node.module)
    return imported_names


def _imported_members(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    imported_members: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_members.extend((node.module, alias.name) for alias in node.names)
    return imported_members


def _package_import_targets(path: Path, modules: set[str]) -> set[str]:
    module_name = _module_name_for_path(path)
    tree = ast.parse(path.read_text(), filename=str(path))
    targets: set[str] = set()
    for node in ast.walk(tree):
        for imported_name in _node_imported_modules(node, module_name):
            target = _resolve_existing_module(imported_name, modules)
            if target is not None and target != module_name:
                targets.add(target)
    return targets


def _node_imported_modules(node: ast.AST, module_name: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [
            alias.name
            for alias in node.names
            if alias.name == "avarch" or alias.name.startswith("avarch.")
        ]
    if isinstance(node, ast.ImportFrom) and node.module is not None:
        if node.level:
            base_parts = module_name.split(".")[: -node.level]
            return [".".join((*base_parts, node.module))]
        return [node.module]
    return []


def _resolve_existing_module(imported_name: str, modules: set[str]) -> str | None:
    if imported_name != "avarch" and not imported_name.startswith("avarch."):
        return None
    parts = imported_name.split(".")
    for length in range(len(parts), 0, -1):
        candidate = ".".join(parts[:length])
        if candidate in modules:
            return candidate
    return None


def _import_cycles(graph: dict[str, set[str]]) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []
    cycles: list[str] = []

    def visit(module_name: str) -> None:
        visiting.add(module_name)
        stack.append(module_name)
        for target in sorted(graph[module_name]):
            if target in visiting:
                cycle_start = stack.index(target)
                cycles.append(" -> ".join([*stack[cycle_start:], target]))
                continue
            if target not in visited:
                visit(target)
        stack.pop()
        visiting.remove(module_name)
        visited.add(module_name)

    for module_name in sorted(graph):
        if module_name not in visited:
            visit(module_name)
    return cycles


def _job_state_assignments(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    assignments: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets.append(node.target)
        for target in targets:
            assignments.extend(_job_state_assignment_targets(target))
    return assignments


def _job_state_assignment_targets(target: ast.expr) -> list[tuple[int, str]]:
    if isinstance(target, ast.Attribute):
        if (
            target.attr in {"status", "stage"}
            and isinstance(target.value, ast.Name)
            and target.value.id == "job"
        ):
            return [(target.lineno, target.attr)]
        return []
    if isinstance(target, (ast.Tuple, ast.List)):
        assignments: list[tuple[int, str]] = []
        for element in target.elts:
            assignments.extend(_job_state_assignment_targets(element))
        return assignments
    return []


def _is_forbidden_import(imported_name: str) -> bool:
    return any(
        imported_name == forbidden or imported_name.startswith(f"{forbidden}.")
        for forbidden in FORBIDDEN_IMPORT_ROOTS
    )
