from typer.testing import CliRunner

from avarch.cli import app

runner = CliRunner()


def test_tui_command_help() -> None:
    result = runner.invoke(app, ["tui", "--help"])

    assert result.exit_code == 0
