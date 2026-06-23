from __future__ import annotations

import os
import subprocess
from pathlib import Path

INSTALL_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"
WRAPPER = Path(__file__).resolve().parents[1] / "bin" / "avarch"


def test_install_script_has_valid_shell_syntax() -> None:
    result = subprocess.run(
        ["sh", "-n", str(INSTALL_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_installer_embeds_default_image_and_installed_command_uses_it(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    result = subprocess.run(
        ["sh", str(INSTALL_SCRIPT)],
        env=_install_env(
            fake_bin,
            log,
            install_dir=install_dir,
            image="example.test/avarch:ci",
            skip_pull=True,
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    installed = install_dir / "avarch"
    assert result.returncode == 0, result.stderr
    assert installed.is_file()
    assert "AVARCH_DEFAULT_IMAGE='example.test/avarch:ci'" in installed.read_text(encoding="utf-8")

    command_result = subprocess.run(
        [str(installed), "version"],
        cwd=tmp_path,
        env=_command_env(fake_bin, log),
        capture_output=True,
        text=True,
        check=False,
    )

    assert command_result.returncode == 0
    assert "example.test/avarch:ci version" in log.read_text(encoding="utf-8")


def test_installer_pulls_image_by_default(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    result = subprocess.run(
        ["sh", str(INSTALL_SCRIPT)],
        env=_install_env(
            fake_bin,
            log,
            install_dir=install_dir,
            image="example.test/avarch:latest",
            skip_pull=False,
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "pull example.test/avarch:latest" in log.read_text(encoding="utf-8")


def test_installer_supports_version_tag_without_full_image_override(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    env = _command_env(fake_bin, log)
    env.update(
        {
            "AVARCH_INSTALL_DIR": str(install_dir),
            "AVARCH_SKIP_PULL": "0",
            "AVARCH_VERSION": "0.1.0",
            "AVARCH_WRAPPER_FILE": str(WRAPPER),
        }
    )

    result = subprocess.run(
        ["sh", str(INSTALL_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "pull docker.io/asmir100/avarch:0.1.0" in log.read_text(encoding="utf-8")


def test_installer_defaults_to_alpha_channel(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    env = _command_env(fake_bin, log)
    env.update(
        {
            "AVARCH_INSTALL_DIR": str(install_dir),
            "AVARCH_SKIP_PULL": "0",
            "AVARCH_WRAPPER_FILE": str(WRAPPER),
        }
    )

    result = subprocess.run(
        ["sh", str(INSTALL_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "pull docker.io/asmir100/avarch:alpha" in log.read_text(encoding="utf-8")


def _fake_docker(root: Path) -> Path:
    fake_bin = root / "fake-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env sh
set -eu

if [ "$1" = "ps" ]; then
  exit 0
fi

if [ "$1" = "inspect" ]; then
  printf '%s\n' "false		"
  exit 0
fi

printf '%s\n' "$*" >> "$DOCKER_LOG"
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return fake_bin


def _install_env(
    fake_bin: Path,
    log: Path,
    *,
    install_dir: Path,
    image: str,
    skip_pull: bool,
) -> dict[str, str]:
    env = _command_env(fake_bin, log)
    env.update(
        {
            "AVARCH_IMAGE": image,
            "AVARCH_INSTALL_DIR": str(install_dir),
            "AVARCH_SKIP_PULL": "1" if skip_pull else "0",
            "AVARCH_WRAPPER_FILE": str(WRAPPER),
        }
    )
    return env


def _command_env(fake_bin: Path, log: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("AVARCH_IMAGE", None)
    env.pop("AVARCH_VERSION", None)
    env.update(
        {
            "DOCKER_LOG": str(log),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    return env
