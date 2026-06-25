from __future__ import annotations

import ast
from pathlib import Path

DOMAIN_ROOT = Path(__file__).resolve().parents[1] / "src" / "avarch" / "domain"
PACKAGE_ROOT = DOMAIN_ROOT.parent
APPLICATION_ROOT = PACKAGE_ROOT / "application"

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
        "execution.py",
        "inventory.py",
        "job_lifecycle.py",
        "planner.py",
        "probe.py",
        "promoter.py",
        "rejection_cleanup.py",
        "scanner.py",
        "scheduler.py",
        "scheduler_lifecycle.py",
        "scheduler_queue.py",
        "scheduler_runner.py",
        "scheduler_support.py",
        "scheduler_workers.py",
        "size_policy.py",
        "validation.py",
        "vapoursynth.py",
        "vpy_env.py",
        "vpy_plugins.py",
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
        "avarch.execution",
        "avarch.inventory",
        "avarch.job_lifecycle",
        "avarch.planner",
        "avarch.probe",
        "avarch.promoter",
        "avarch.rejection_cleanup",
        "avarch.scanner",
        "avarch.scheduler_lifecycle",
        "avarch.scheduler_runner",
        "avarch.scheduler_support",
        "avarch.scheduler_workers",
        "avarch.models.db",
        "avarch.models.scheduler",
        "avarch.size_policy",
        "avarch.validation",
        "avarch.vapoursynth",
        "avarch.vpy_env",
        "avarch.vpy_plugins",
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
            "claim_recoverable_promotion",
            "PromotionClaimData",
            "PromotionClaimPersistenceError",
            "PromotedMediaFileSnapshot",
            "commit_verified_promotion",
            "mark_promotion_rolled_back",
            "persist_promotion_claim",
            "recoverable_promotion",
            "release_promotion_lease",
            "require_promotion_job",
            "require_promotion_record",
            "renew_promotion_lease",
            "set_promotion_phase",
            "update_promotion_cleanup",
            "update_promotion_staged",
            "update_promotion_verified",
            "write_promotion_journal",
        }
    ),
    "avarch.scheduler": frozenset(
        {
            "JobControlError",
            "RecoverySummary",
            "SCHEDULER_LEASE_SECONDS",
            "SchedulerAlreadyRunningError",
            "SchedulerControlError",
            "SchedulerLeaseLostError",
            "acknowledge_scheduler_control",
            "acquire_scheduler_lease",
            "add_job_event",
            "attach_probe_and_advance",
            "cancel_job",
            "claimable_jobs",
            "claim_job_stage",
            "clear_cancel_fields",
            "clear_hold_fields",
            "complete_job_stage",
            "create_queue_job",
            "drain_scheduler",
            "fail_job_after_external",
            "fail_job_stage",
            "has_resource_capacity",
            "hold_job",
            "interrupt_job_stage",
            "pause_scheduler",
            "release_scheduler_lease",
            "release_job",
            "recover_abandoned_jobs",
            "renew_scheduler_lease",
            "request_job_retry",
            "reset_job_for_retry",
            "reset_retry_job",
            "resource_for_stage",
            "resume_scheduler",
            "skip_claimed_job",
            "stop_scheduler",
            "update_job_priority",
        }
    ),
    "avarch.scheduler_queue": frozenset(
        {
            "QueueClearSummary",
            "QueueRetrySummary",
            "RetrySummary",
            "clear_queue",
            "retry_failed_jobs",
            "retry_job",
            "retry_queue",
        }
    ),
}

OBSOLETE_FUNCTION_DEFINITIONS = {
    "avarch.promoter": frozenset(
        {
            "_recoverable_promotion",
            "_require_attempt",
            "_require_job",
            "_require_promotion",
            "mark_promotion_rolled_back",
            "commit_verified_promotion",
            "_set_phase",
            "release_promotion_lease",
            "renew_promotion_lease",
            "set_promotion_phase",
            "update_promotion_cleanup",
            "update_promotion_staged",
            "update_promotion_verified",
        }
    ),
    "avarch.scheduler": frozenset(
        {
            "_active_status_for_stage",
            "_acknowledge_scheduler_control",
            "_add_job_event",
            "_attach_probe_and_advance_row",
            "_cancel_claimed_job",
            "_clear_cancel_fields",
            "_clear_hold_fields",
            "_datetime_after",
            "_fail_after_external",
            "_get_or_create_scheduler_state",
            "_interrupt_running_job",
            "_lease_active",
            "_request_scheduler_mode",
            "_require_attempt",
            "_require_job",
            "_require_media_file",
            "_reset_job_for_retry",
            "_mark_job_skipped",
            "_skip_claimed_job",
            "_job_can_be_claimed_for_stage",
            "acknowledge_scheduler_control",
            "acquire_scheduler_lease",
            "add_job_event",
            "attach_probe_and_advance",
            "cancel_job",
            "cancel_claimed_job",
            "claimable_jobs",
            "claim_job_stage",
            "clear_cancel_fields",
            "clear_hold_fields",
            "complete_job_stage",
            "create_queue_job",
            "drain_scheduler",
            "fail_job_after_external",
            "fail_job_stage",
            "has_resource_capacity",
            "hold_job",
            "interrupt_job_stage",
            "pause_scheduler",
            "release_scheduler_lease",
            "release_job",
            "recover_abandoned_jobs",
            "renew_scheduler_lease",
            "request_job_retry",
            "reset_job_for_retry",
            "reset_retry_job",
            "resource_for_stage",
            "resume_scheduler",
            "skip_claimed_job",
            "stop_scheduler",
            "update_job_priority",
        }
    ),
    "avarch.scheduler_queue": frozenset(
        {
            "clear_queue",
            "retry_failed_jobs",
            "retry_job",
            "retry_queue",
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


def test_obsolete_function_definitions_are_not_reintroduced() -> None:
    violations: list[str] = []
    for module_name, obsolete_functions in OBSOLETE_FUNCTION_DEFINITIONS.items():
        path = _module_path(module_name)
        if not path.exists():
            continue
        defined_functions = set(_defined_functions(path))
        for function_name in sorted(obsolete_functions & defined_functions):
            relative_path = path.relative_to(PACKAGE_ROOT.parent.parent)
            violations.append(f"{relative_path} defines {function_name}")

    assert violations == []


def test_validation_shell_module_stays_deleted() -> None:
    assert not (PACKAGE_ROOT / "validation.py").exists()


def test_application_modules_do_not_import_concrete_boundaries() -> None:
    forbidden_roots = frozenset(
        {
            "avarch.adapters",
            "avarch.adapters.scheduler_support",
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


def _domain_module_paths() -> list[Path]:
    return sorted(path for path in DOMAIN_ROOT.rglob("*.py") if path.is_file())


def _application_module_paths() -> list[Path]:
    if not APPLICATION_ROOT.exists():
        return []
    return sorted(path for path in APPLICATION_ROOT.rglob("*.py") if path.is_file())


def _package_module_paths() -> list[Path]:
    return sorted(path for path in PACKAGE_ROOT.rglob("*.py") if path.is_file())


def _test_module_paths() -> list[Path]:
    test_root = PACKAGE_ROOT.parent.parent / "tests"
    return sorted(path for path in test_root.rglob("*.py") if path.is_file())


def _module_path(module_name: str) -> Path:
    if not module_name.startswith("avarch."):
        raise ValueError(f"Unsupported module: {module_name}")
    relative_module = module_name.removeprefix("avarch.").replace(".", "/")
    return PACKAGE_ROOT / f"{relative_module}.py"


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


def _defined_functions(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    return [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]


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
