from pathlib import Path

from typer.testing import CliRunner

from avarch.cli import app

runner = CliRunner()


def test_db_upgrade_after_init(tmp_path: Path) -> None:
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0

    upgrade_result = runner.invoke(
        app,
        ["db", "upgrade"],
    )

    assert upgrade_result.exit_code == 0


def test_db_current_after_init(tmp_path: Path) -> None:
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0

    current_result = runner.invoke(
        app,
        ["db", "current"],
    )

    assert current_result.exit_code == 0
    assert current_result.output.strip()
