from pathlib import Path

import sqlalchemy as sa
from typer.testing import CliRunner

from avarch.cli import app
from avarch.db import create_db_engine

runner = CliRunner()


def test_db_upgrade_after_init(tmp_path: Path) -> None:
    config_path = tmp_path / "avarch.toml"

    init_result = runner.invoke(app, ["init", "--config", str(config_path)])
    assert init_result.exit_code == 0

    upgrade_result = runner.invoke(
        app,
        ["db", "upgrade", "--config", str(config_path)],
    )

    assert upgrade_result.exit_code == 0


def test_db_current_after_init(tmp_path: Path) -> None:
    config_path = tmp_path / "avarch.toml"

    init_result = runner.invoke(app, ["init", "--config", str(config_path)])
    assert init_result.exit_code == 0

    current_result = runner.invoke(
        app,
        ["db", "current", "--config", str(config_path)],
    )

    assert current_result.exit_code == 0
    assert current_result.output.strip()


def test_db_upgrade_rejects_old_revision_without_traceback(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    config_path = tmp_path / "avarch.toml"
    config_path.write_text(
        f'[database]\nurl = "sqlite:///{db_path}"\n',
        encoding="utf-8",
    )
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR)"))
        connection.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES ('9aaf75d07ce6')")
        )

    result = runner.invoke(app, ["db", "upgrade", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "unsupported development schema" in result.output
    assert "rm -rf .avarch" in result.output
    assert "Traceback" not in result.output
