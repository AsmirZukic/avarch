from pathlib import Path

from typer.testing import CliRunner

from avarch.cli import app

runner = CliRunner()


def test_init_creates_config_and_database(tmp_path: Path) -> None:
    config_path = tmp_path / "avarch.toml"

    result = runner.invoke(app, ["init", "--config", str(config_path)])

    assert result.exit_code == 0
    assert config_path.exists()

    config_text = config_path.read_text(encoding="utf-8")
    assert "[database]" in config_text
    assert "[logging]" in config_text
    assert 'format = "console"' in config_text

    db_path = tmp_path / ".avarch" / "avarch.db"
    assert db_path.exists()


def test_init_does_not_overwrite_existing_config(tmp_path: Path) -> None:
    config_path = tmp_path / "avarch.toml"
    config_path.write_text("custom = true\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "--config", str(config_path)])

    assert result.exit_code != 0
    assert config_path.read_text(encoding="utf-8") == "custom = true\n"


def test_init_force_overwrites_existing_config(tmp_path: Path) -> None:
    config_path = tmp_path / "avarch.toml"
    config_path.write_text("custom = true\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "--config", str(config_path), "--force"])

    assert result.exit_code == 0
    assert "[database]" in config_path.read_text(encoding="utf-8")
