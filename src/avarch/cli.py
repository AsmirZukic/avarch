from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import structlog
import typer
from sqlalchemy import inspect
from sqlmodel import Session, select

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
from avarch.models.db import MediaFile, MediaFileStatus
from avarch.probe import (
    ProbeError,
    format_probe_summary,
    get_canonical_probe_result,
    normalize_probe,
    parse_normalized_probe_json,
    run_ffprobe,
    store_probe_result,
)
from avarch.scanner import ScanError, ScanResult, scan_root, update_inventory
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
RootsArgument = Annotated[
    list[Path] | None,
    typer.Argument(help="Media roots to scan."),
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

    _write_default_config(config)

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
def scan(
    roots: RootsArgument = None,
    config: ConfigOption = Path("avarch.toml"),
) -> None:
    config = _resolve_cli_path(config)
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    upgrade_database(database_url)

    roots_to_scan = list(roots or app_config.scanner.roots)
    if not roots_to_scan:
        typer.echo("No scan roots provided or configured.")
        raise typer.Exit(1)

    engine = create_db_engine(database_url)
    results: list[ScanResult] = []
    for root in roots_to_scan:
        root = _resolve_cli_path(root)
        log.info("scan_started", root=str(root))
        try:
            snapshots = scan_root(
                root,
                extensions=app_config.scanner.extensions,
                exclude_directories=app_config.scanner.exclude_directories,
            )
            with Session(engine) as session, session.begin():
                result = update_inventory(
                    session,
                    root=root,
                    snapshots=snapshots,
                    scanned_at=_utc_now(),
                )
            results.append(result)
        except ScanError as exc:
            log.error("scan_failed", root=str(root), reason=str(exc))
            typer.echo(str(exc))
            raise typer.Exit(1) from exc
        log.info("scan_completed", root=str(root))
        _echo_scan_result(result)

    if len(results) > 1:
        typer.echo("Total")
        _echo_scan_counts(
            added=sum(result.added for result in results),
            changed=sum(result.changed for result in results),
            missing=sum(result.missing for result in results),
            unchanged=sum(result.unchanged for result in results),
        )

    typer.echo("Scan complete.")


@app.command()
def files(
    changed: Annotated[
        bool,
        typer.Option("--changed", help="Show added, changed, and missing files only."),
    ] = False,
    config: ConfigOption = Path("avarch.toml"),
) -> None:
    config = _resolve_cli_path(config)
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    upgrade_database(database_url)

    engine = create_db_engine(database_url)
    with Session(engine) as session:
        statement = select(MediaFile).order_by(MediaFile.path)
        media_files = list(session.exec(statement).all())

    if changed:
        media_files = [
            media_file
            for media_file in media_files
            if _status_value(media_file.status)
            in {
                MediaFileStatus.ADDED.value,
                MediaFileStatus.CHANGED.value,
                MediaFileStatus.MISSING.value,
            }
        ]

    typer.echo("STATUS   SIZE        PATH")
    for media_file in media_files:
        typer.echo(
            f"{_status_value(media_file.status):<8} {_format_size(media_file.size_bytes):>10}  "
            f"{media_file.path}"
        )


@app.command("probe")
def probe_file(
    file: Annotated[Path, typer.Argument(help="Tracked media file to probe.")],
    config: ConfigOption = Path("avarch.toml"),
) -> None:
    config = _resolve_cli_path(config)
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    upgrade_database(database_url)

    file_path = _resolve_media_path(file)
    if not file_path.exists():
        typer.echo(f"File does not exist: {file_path}")
        raise typer.Exit(1)
    if not file_path.is_file():
        typer.echo(f"Path is not a regular file: {file_path}")
        raise typer.Exit(1)

    engine = create_db_engine(database_url)
    with Session(engine) as session:
        media_file = _get_media_file(session, file_path)
        if media_file is None:
            _echo_untracked_file()
            raise typer.Exit(1)
        if _status_value(media_file.status) == MediaFileStatus.MISSING.value:
            typer.echo("File is marked missing in the media inventory.")
            typer.echo("Run avarch scan for the containing root first.")
            raise typer.Exit(1)

        try:
            raw_probe = run_ffprobe(file_path)
            normalized_probe = normalize_probe(raw_probe)
            probe_result = store_probe_result(
                session,
                media_file=media_file,
                raw_probe=raw_probe,
                normalized_probe=normalized_probe,
                created_at=_utc_now(),
            )
            session.commit()
            session.refresh(probe_result)
        except ProbeError as exc:
            session.rollback()
            typer.echo(str(exc))
            raise typer.Exit(1) from exc

    typer.echo(
        format_probe_summary(
            file_path,
            normalized_probe,
            probe_result.probe_hash,
        )
    )


@app.command("inspect")
def inspect_file(
    file: Annotated[Path, typer.Argument(help="Tracked media file to inspect.")],
    config: ConfigOption = Path("avarch.toml"),
) -> None:
    config = _resolve_cli_path(config)
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    upgrade_database(database_url)

    file_path = _resolve_media_path(file)
    engine = create_db_engine(database_url)
    with Session(engine) as session:
        media_file = _get_media_file(session, file_path)
        if media_file is None:
            _echo_untracked_file()
            raise typer.Exit(1)
        if media_file.id is None:
            typer.echo("File is not present in the media inventory.")
            raise typer.Exit(1)

        probe_result = get_canonical_probe_result(session, media_file)
        if probe_result is None:
            typer.echo("No stored probe result exists for this file.")
            raise typer.Exit(1)

        try:
            normalized_probe = parse_normalized_probe_json(probe_result.normalized_json)
        except ProbeError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1) from exc

    typer.echo(format_probe_summary(file_path, normalized_probe, probe_result.probe_hash))


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
    config_created = False
    if not config.exists():
        _write_default_config(config)
        config_created = True

    app_config = load_config(config)
    configure_logging(app_config.logging.level, app_config.logging.format)
    if config_created:
        log.info("config_created", path=str(config))
    log.info("config_loaded", path=str(config))

    data_dir = resolve_data_dir(app_config, config)
    data_dir.mkdir(parents=True, exist_ok=True)

    return app_config


def _write_default_config(config: Path) -> None:
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")


def _resolve_cli_path(path: Path) -> Path:
    if path.is_absolute():
        return path

    original_pwd = os.environ.get("PWD")
    if original_pwd and Path(original_pwd) != Path.cwd():
        return Path(original_pwd) / path

    return path


def _resolve_media_path(path: Path) -> Path:
    return _resolve_cli_path(path).resolve()


def _get_media_file(session: Session, path: Path) -> MediaFile | None:
    statement = select(MediaFile).where(MediaFile.path == str(path))
    return session.exec(statement).first()


def _echo_untracked_file() -> None:
    typer.echo("File is not present in the media inventory.")
    typer.echo("Run avarch scan for the containing root first.")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _echo_scan_result(result: ScanResult) -> None:
    typer.echo(f"Scanning {result.root}")
    _echo_scan_counts(
        added=result.added,
        changed=result.changed,
        missing=result.missing,
        unchanged=result.unchanged,
    )
    typer.echo("")


def _echo_scan_counts(*, added: int, changed: int, missing: int, unchanged: int) -> None:
    typer.echo(f"Added:     {added}")
    typer.echo(f"Changed:   {changed}")
    typer.echo(f"Missing:   {missing}")
    typer.echo(f"Unchanged: {unchanged}")


def _format_size(size_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(size_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def _status_value(status: MediaFileStatus | str) -> str:
    if isinstance(status, MediaFileStatus):
        return status.value
    return status


app.add_typer(db_app, name="db")
