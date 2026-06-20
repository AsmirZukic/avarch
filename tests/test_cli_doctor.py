from pathlib import Path

from typer.testing import CliRunner

from avarch.cli import app

runner = CliRunner()


def test_doctor_passes_after_init(tmp_path: Path) -> None:
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0

    doctor_result = runner.invoke(app, ["doctor"])

    assert doctor_result.exit_code == 0
    assert "PASS" in doctor_result.output


def test_doctor_fails_for_missing_config(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code != 0
    assert "No Avarch workspace found" in result.output


def test_doctor_accepts_json_log_format(tmp_path: Path) -> None:
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0

    doctor_result = runner.invoke(
        app,
        ["doctor", "--log-format", "json"],
    )

    assert doctor_result.exit_code == 0
