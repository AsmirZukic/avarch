from __future__ import annotations

import os
import subprocess
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "bin" / "avarch"


def test_wrapper_runs_version_without_workspace(tmp_path: Path) -> None:
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "version"],
        cwd=tmp_path,
        env=_wrapper_env(fake_bin, log, image="avarch:test"),
        capture_output=True,
        text=True,
        check=False,
    )

    command = log.read_text(encoding="utf-8")
    assert result.returncode == 0
    assert "-v " not in command
    assert "avarch:test version" in command


def test_wrapper_mounts_current_directory_for_init_without_workspace(tmp_path: Path) -> None:
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "init"],
        cwd=tmp_path,
        env=_wrapper_env(fake_bin, log, image="avarch:test"),
        capture_output=True,
        text=True,
        check=False,
    )

    command = log.read_text(encoding="utf-8")
    assert result.returncode == 0
    assert f"-v {tmp_path}:/workspace" in command
    assert "-w /workspace" in command
    assert "avarch:test init" in command


def test_wrapper_still_requires_workspace_for_workspace_commands(tmp_path: Path) -> None:
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "scan", "."],
        cwd=tmp_path,
        env=_wrapper_env(fake_bin, log, image="avarch:test"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "No Avarch workspace found" in result.stderr
    assert not log.exists()


def test_wrapper_discovers_workspace_and_forwards_exit_code(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    nested = workspace / "Movies"
    nested.mkdir()
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "version"],
        cwd=nested,
        env=_wrapper_env(fake_bin, log, image="avarch:test", docker_exit_code="37"),
        capture_output=True,
        text=True,
        check=False,
    )

    command = log.read_text(encoding="utf-8")
    assert result.returncode == 37
    assert f"-v {workspace}:/workspace" in command
    assert "-w /workspace" in command
    assert "avarch:test version" in command


def test_wrapper_adds_selinux_security_option_when_workspace_has_selinux_context(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "version"],
        cwd=workspace,
        env=_wrapper_env(
            fake_bin,
            log,
            image="avarch:test",
            ls_zd_result="unconfined_u:object_r:user_home_t:s0",
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    command = log.read_text(encoding="utf-8")
    assert result.returncode == 0
    assert "--security-opt label=disable" in command
    assert f"-v {workspace}:/workspace" in command


def test_wrapper_security_opt_env_overrides_selinux_detection(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "version"],
        cwd=workspace,
        env=_wrapper_env(
            fake_bin,
            log,
            image="avarch:test",
            security_opt="",
            ls_zd_result="unconfined_u:object_r:user_home_t:s0",
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    command = log.read_text(encoding="utf-8")
    assert result.returncode == 0
    assert "--security-opt label=disable" not in command
    assert f"-v {workspace}:/workspace " in command


def test_wrapper_volume_options_env_can_request_relabel(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "version"],
        cwd=workspace,
        env=_wrapper_env(
            fake_bin,
            log,
            image="avarch:test",
            security_opt="",
            volume_options="z",
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    command = log.read_text(encoding="utf-8")
    assert result.returncode == 0
    assert f"-v {workspace}:/workspace:z" in command


def test_wrapper_rejects_running_scheduler_container(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "scheduler", "run"],
        cwd=workspace,
        env=_wrapper_env(
            fake_bin,
            log,
            docker_ps_id="abc123",
            docker_inspect_result=f"true\tencoder\t{workspace}",
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "already running" in result.stderr
    assert str(workspace) in result.stderr


def test_wrapper_removes_stale_avarch_scheduler_container(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "scheduler", "run"],
        cwd=workspace,
        env=_wrapper_env(
            fake_bin,
            log,
            docker_ps_id="abc123",
            docker_inspect_result=f"false\tencoder\t{workspace}",
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    command_log = log.read_text(encoding="utf-8")
    assert result.returncode == 0
    assert "rm abc123" in command_log
    assert "--name avarch-encoder" in command_log
    assert f"--label org.avarch.workspace={workspace}" in command_log


def test_wrapper_preserves_foreign_scheduler_container(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "scheduler", "run"],
        cwd=workspace,
        env=_wrapper_env(
            fake_bin,
            log,
            docker_ps_id="abc123",
            docker_inspect_result="false\t\t",
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "non-Avarch container" in result.stderr
    assert not log.exists() or "rm abc123" not in log.read_text(encoding="utf-8")


def _workspace(root: Path) -> Path:
    workspace = root / "workspace"
    (workspace / ".avarch").mkdir(parents=True)
    (workspace / ".avarch" / "workspace.toml").write_text(
        'schema_version = 1\nid = "test"\n',
        encoding="utf-8",
    )
    return workspace


def _fake_docker(root: Path) -> Path:
    fake_bin = root / "fake-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env sh
set -eu

if [ "$1" = "ps" ]; then
  if [ -n "${DOCKER_PS_ID:-}" ]; then
    printf '%s\\n' "$DOCKER_PS_ID"
  fi
  exit 0
fi

if [ "$1" = "inspect" ]; then
  printf '%s\\n' "${DOCKER_INSPECT_RESULT:-false		}"
  exit 0
fi

printf '%s\\n' "$*" >> "$DOCKER_LOG"
exit "${DOCKER_EXIT_CODE:-0}"
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    ls = fake_bin / "ls"
    ls.write_text(
        """#!/usr/bin/env sh
set -eu

if [ "${1:-}" = "-Zd" ]; then
  if [ -n "${LS_ZD_RESULT:-}" ]; then
    printf '%s\\n' "$LS_ZD_RESULT"
    exit 0
  fi
  exit 1
fi

exit 1
""",
        encoding="utf-8",
    )
    ls.chmod(0o755)
    return fake_bin


def _wrapper_env(
    fake_bin: Path,
    log: Path,
    *,
    image: str = "avarch:test",
    docker_exit_code: str = "0",
    docker_ps_id: str = "",
    docker_inspect_result: str = "false\t\t",
    ls_zd_result: str = "",
    security_opt: str | None = None,
    volume_options: str | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AVARCH_IMAGE": image,
            "DOCKER_EXIT_CODE": docker_exit_code,
            "DOCKER_INSPECT_RESULT": docker_inspect_result,
            "DOCKER_LOG": str(log),
            "DOCKER_PS_ID": docker_ps_id,
            "LS_ZD_RESULT": ls_zd_result,
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    if security_opt is not None:
        env["AVARCH_DOCKER_SECURITY_OPT"] = security_opt
    if volume_options is not None:
        env["AVARCH_DOCKER_VOLUME_OPTIONS"] = volume_options
    return env
