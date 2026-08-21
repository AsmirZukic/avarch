import pytest
from typer.testing import CliRunner

from avarch.cli import app

runner = CliRunner()


def test_cli_help_exits_successfully() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "avarch" in result.output.lower()


def test_cli_version_exits_successfully() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "0.2.0" in result.output


def test_cli_version_command_exits_successfully() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "0.2.0" in result.output


@pytest.mark.parametrize(
    "command",
    [
        ["init"],
        ["doctor"],
        ["scan"],
        ["files", "list"],
        ["probe"],
        ["files", "show"],
        ["plan"],
        ["promote"],
        ["workflow"],
        ["workflow", "run"],
        ["system"],
        ["system", "resources"],
    ],
)
def test_user_can_check_help_for_each_command(command: list[str]) -> None:
    result = runner.invoke(app, [*command, "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_system_resources_reports_detected_envelope() -> None:
    result = runner.invoke(app, ["system", "resources"])

    assert result.exit_code == 0
    assert "Resources" in result.output
    assert "effective CPUs" in result.output
    assert "effective memory" in result.output
