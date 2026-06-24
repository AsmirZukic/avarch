from __future__ import annotations

import ast
from pathlib import Path

DOMAIN_ROOT = Path(__file__).resolve().parents[1] / "src" / "avarch" / "domain"
PACKAGE_ROOT = DOMAIN_ROOT.parent

FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "alembic",
        "avarch.adapters",
        "avarch.application",
        "avarch.cli",
        "avarch.adapters.sqlite.db",
        "avarch.adapters.sqlite.migrations",
        "avarch.execution",
        "avarch.models",
        "avarch.promoter",
        "avarch.scheduler",
        "avarch.scheduler_lifecycle",
        "pydantic",
        "sqlalchemy",
        "sqlmodel",
        "structlog",
        "subprocess",
        "typer",
    }
)

OBSOLETE_TOP_LEVEL_MODULES = frozenset(
    {
        "db.py",
        "db_migrations.py",
        "inventory.py",
        "job_lifecycle.py",
        "rejection_cleanup.py",
        "size_policy.py",
    }
)

OBSOLETE_MODEL_MODULES = frozenset(
    {
        "db.py",
        "scheduler.py",
    }
)

OBSOLETE_IMPORT_ROOTS = frozenset(
    {
        "avarch.db",
        "avarch.db_migrations",
        "avarch.inventory",
        "avarch.job_lifecycle",
        "avarch.rejection_cleanup",
        "avarch.models.db",
        "avarch.models.scheduler",
        "avarch.size_policy",
    }
)

OBSOLETE_IMPORT_MEMBERS = {
    "avarch.config": frozenset(
        {
            "resolve_database_url",
        }
    ),
    "avarch.probe": frozenset(
        {
            "get_canonical_probe_result",
            "store_probe_result",
        }
    ),
    "avarch.scanner": frozenset(
        {
            "update_inventory",
        }
    ),
    "avarch.validation": frozenset(
        {
            "persist_validation_result",
        }
    ),
    "avarch.planner": frozenset(
        {
            "PlanArtifactConflictError",
            "load_planning_context",
            "write_plan_artifacts",
        }
    ),
    "avarch.promoter": frozenset(
        {
            "calculate_promotion_digest",
            "release_promotion_lease",
            "renew_promotion_lease",
            "write_promotion_journal",
        }
    ),
    "avarch.scheduler": frozenset(
        {
            "claimable_jobs",
            "has_resource_capacity",
            "resource_for_stage",
        }
    ),
}


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


def test_obsolete_top_level_modules_stay_deleted() -> None:
    obsolete_paths = [PACKAGE_ROOT / name for name in OBSOLETE_TOP_LEVEL_MODULES]
    obsolete_paths.extend(PACKAGE_ROOT / "models" / name for name in OBSOLETE_MODEL_MODULES)

    assert [path.relative_to(PACKAGE_ROOT) for path in obsolete_paths if path.exists()] == []


def test_obsolete_import_paths_are_not_used() -> None:
    violations: list[str] = []
    for path in _package_module_paths() + _test_module_paths():
        for imported_name in _imported_names(path):
            if _is_obsolete_import(imported_name):
                relative_path = path.relative_to(PACKAGE_ROOT.parent.parent)
                violations.append(f"{relative_path} imports {imported_name}")

    assert violations == []


def test_obsolete_import_members_are_not_used() -> None:
    violations: list[str] = []
    for path in _package_module_paths() + _test_module_paths():
        for module_name, imported_member in _imported_members(path):
            obsolete_members = OBSOLETE_IMPORT_MEMBERS.get(module_name, frozenset())
            if imported_member in obsolete_members:
                relative_path = path.relative_to(PACKAGE_ROOT.parent.parent)
                violations.append(f"{relative_path} imports {module_name}.{imported_member}")

    assert violations == []


def test_validation_module_does_not_import_adapters() -> None:
    imported_adapters = sorted(
        imported_name
        for imported_name in _imported_names(PACKAGE_ROOT / "validation.py")
        if imported_name == "avarch.adapters" or imported_name.startswith("avarch.adapters.")
    )

    assert imported_adapters == []


def test_config_module_does_not_import_sqlalchemy() -> None:
    imported_sqlalchemy = sorted(
        imported_name
        for imported_name in _imported_names(PACKAGE_ROOT / "config.py")
        if imported_name == "sqlalchemy" or imported_name.startswith("sqlalchemy.")
    )

    assert imported_sqlalchemy == []


def _domain_module_paths() -> list[Path]:
    return sorted(path for path in DOMAIN_ROOT.rglob("*.py") if path.is_file())


def _package_module_paths() -> list[Path]:
    return sorted(path for path in PACKAGE_ROOT.rglob("*.py") if path.is_file())


def _test_module_paths() -> list[Path]:
    test_root = PACKAGE_ROOT.parent.parent / "tests"
    return sorted(path for path in test_root.rglob("*.py") if path.is_file())


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


def _is_forbidden_import(imported_name: str) -> bool:
    return any(
        imported_name == forbidden or imported_name.startswith(f"{forbidden}.")
        for forbidden in FORBIDDEN_IMPORT_ROOTS
    )


def _is_obsolete_import(imported_name: str) -> bool:
    return any(
        imported_name == obsolete or imported_name.startswith(f"{obsolete}.")
        for obsolete in OBSOLETE_IMPORT_ROOTS
    )
