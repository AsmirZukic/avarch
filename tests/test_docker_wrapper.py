from __future__ import annotations

import os
import subprocess
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "bin" / "avarch"


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


def test_wrapper_adds_gpu_options(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    log = tmp_path / "docker.log"
    fake_bin = _fake_docker(tmp_path)

    result = subprocess.run(
        [str(WRAPPER), "doctor"],
        cwd=workspace,
        env=_wrapper_env(fake_bin, log, gpu="nvidia"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--gpus all" in log.read_text(encoding="utf-8")


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
    return fake_bin


def _wrapper_env(
    fake_bin: Path,
    log: Path,
    *,
    image: str = "avarch:test",
    docker_exit_code: str = "0",
    docker_ps_id: str = "",
    docker_inspect_result: str = "false\t\t",
    gpu: str = "",
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AVARCH_IMAGE": image,
            "DOCKER_EXIT_CODE": docker_exit_code,
            "DOCKER_INSPECT_RESULT": docker_inspect_result,
            "DOCKER_LOG": str(log),
            "DOCKER_PS_ID": docker_ps_id,
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
        }
    )
    if gpu:
        env["AVARCH_GPU"] = gpu
    else:
        env.pop("AVARCH_GPU", None)
    return env
