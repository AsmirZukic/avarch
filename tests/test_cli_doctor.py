from pathlib import Path

from typer.testing import CliRunner

from avarch.cli import app

runner = CliRunner()


def test_doctor_passes_after_init(tmp_path: Path) -> None:
    config_path = tmp_path / "avarch.toml"

    init_result = runner.invoke(app, ["init", "--config", str(config_path)])
    assert init_result.exit_code == 0

    doctor_result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert doctor_result.exit_code == 0
    assert "PASS" in doctor_result.output


def test_doctor_fails_for_missing_config(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.toml"

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "missing" in result.output.lower()


def test_doctor_accepts_json_log_format(tmp_path: Path) -> None:
    config_path = tmp_path / "avarch.toml"

    init_result = runner.invoke(app, ["init", "--config", str(config_path)])
    assert init_result.exit_code == 0

    doctor_result = runner.invoke(
        app,
        ["doctor", "--config", str(config_path), "--log-format", "json"],
    )

    assert doctor_result.exit_code == 0
