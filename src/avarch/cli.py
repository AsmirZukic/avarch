from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, Literal

import structlog
import typer
from sqlalchemy import inspect

from avarch import __version__
from avarch.config import (
    DEFAULT_CONFIG_TEXT,
    AppConfig,
    load_config,
    resolve_data_dir,
    resolve_database_url,
)
from avarch.db import create_db_engine
from avarch.db_migrations import get_current_revision, upgrade_database
from avarch.logging import configure_logging
from avarch.tui.app import AvarchTuiApp

log = structlog.get_logger(__name__)

app = typer.Typer(
    name="avarch",
    help="Av1an-first archival transcoding orchestrator.",
    no_args_is_help=True,
)
db_app = typer.Typer(help="Database commands.")


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


VersionOption = Annotated[
    bool,
    typer.Option(
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
]
ConfigOption = Annotated[Path, typer.Option("--config", help="Config file path.")]
ForceOption = Annotated[bool, typer.Option("--force", help="Overwrite existing config.")]
LogFormatOption = Annotated[
    Literal["console", "json"] | None,
    typer.Option("--log-format", help="Override configured log format."),
]


@app.callback()
def main(version: VersionOption = False) -> None:
    pass


@app.command()
def init(config: ConfigOption = Path("avarch.toml"), force: ForceOption = False) -> None:
    config = _resolve_cli_path(config)
    if config.exists() and not force:
        typer.echo(f"Config already exists: {config}")
        raise typer.Exit(1)

    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")

    app_config = load_config(config)
    configure_logging(app_config.logging.level, app_config.logging.format)
    log.info("config_loaded", path=str(config))

    data_dir = resolve_data_dir(app_config, config)
    data_dir.mkdir(parents=True, exist_ok=True)

    database_url = resolve_database_url(app_config, config)
    upgrade_database(database_url)
    log.info("database_upgraded", database_url=database_url)

    typer.echo(f"Config: {config}")
    typer.echo(f"Data dir: {data_dir}")


@db_app.command("upgrade")
def db_upgrade(config: ConfigOption = Path("avarch.toml")) -> None:
    config = _resolve_cli_path(config)
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    upgrade_database(database_url)
    log.info("database_upgraded", database_url=database_url)
    typer.echo("Database upgraded")


@db_app.command("current")
def db_current(config: ConfigOption = Path("avarch.toml")) -> None:
    config = _resolve_cli_path(config)
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    revision = get_current_revision(database_url)
    typer.echo(revision or "unknown")


@app.command()
def doctor(
    config: ConfigOption = Path("avarch.toml"),
    log_format: LogFormatOption = None,
) -> None:
    config = _resolve_cli_path(config)
    if not config.exists():
        configure_logging()
        _doctor_fail("config_exists", "missing config")
        typer.echo(f"FAIL config_exists missing: {config}")
        raise typer.Exit(1)

    try:
        app_config = load_config(config)
    except Exception as exc:
        configure_logging()
        _doctor_fail("config_parses", exc.__class__.__name__)
        typer.echo("FAIL config_parses")
        raise typer.Exit(1) from exc

    effective_log_format = log_format or app_config.logging.format
    configure_logging(app_config.logging.level, effective_log_format)

    _doctor_pass("config_exists")
    typer.echo("PASS config_exists")
    _doctor_pass("config_parses")
    typer.echo("PASS config_parses")

    data_dir = resolve_data_dir(app_config, config)
    if not data_dir.exists():
        _doctor_fail("data_dir_exists", "missing data directory")
        typer.echo("FAIL data_dir_exists")
        raise typer.Exit(1)
    _doctor_pass("data_dir_exists")
    typer.echo("PASS data_dir_exists")

    database_url = resolve_database_url(app_config, config)
    if not database_url.startswith("sqlite:///"):
        _doctor_fail("database_url_sqlite", "database URL is not SQLite")
        typer.echo("FAIL database_url_sqlite")
        raise typer.Exit(1)
    _doctor_pass("database_url_sqlite")
    typer.echo("PASS database_url_sqlite")

    engine = create_db_engine(database_url)
    try:
        with engine.connect():
            pass
    except Exception as exc:
        _doctor_fail("database_connection", exc.__class__.__name__)
        typer.echo("FAIL database_connection")
        raise typer.Exit(1) from exc
    _doctor_pass("database_connection")
    typer.echo("PASS database_connection")

    tables = set(inspect(engine).get_table_names())
    required_tables = {"appmeta", "mediafile"}
    missing_tables = required_tables - tables
    if missing_tables:
        missing = ", ".join(sorted(missing_tables))
        _doctor_fail("required_tables", f"missing table: {missing}")
        typer.echo("FAIL required_tables")
        raise typer.Exit(1)
    _doctor_pass("required_tables")
    typer.echo("PASS required_tables")

    if not __version__:
        _doctor_fail("app_version", "missing version")
        typer.echo("FAIL app_version")
        raise typer.Exit(1)
    _doctor_pass("app_version")
    typer.echo("PASS app_version")


@app.command()
def tui(config: ConfigOption = Path("avarch.toml")) -> None:
    config = _resolve_cli_path(config)
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    data_dir = resolve_data_dir(app_config, config)
    log.info("tui_started")
    AvarchTuiApp(
        initialized=data_dir.exists(),
        database_url=database_url,
        exit_after_mount=not sys.stdin.isatty(),
    ).run(headless=not sys.stdin.isatty())


def _doctor_pass(check: str) -> None:
    log.info("doctor_check_passed", check=check)


def _doctor_fail(check: str, reason: str) -> None:
    log.error("doctor_check_failed", check=check, reason=reason)


def _load_and_configure(config: Path) -> AppConfig:
    app_config = load_config(config)
    configure_logging(app_config.logging.level, app_config.logging.format)
    log.info("config_loaded", path=str(config))
    return app_config


def _resolve_cli_path(path: Path) -> Path:
    if path.is_absolute():
        return path

    original_pwd = os.environ.get("PWD")
    if original_pwd and Path(original_pwd) != Path.cwd():
        return Path(original_pwd) / path

    return path


app.add_typer(db_app, name="db")
