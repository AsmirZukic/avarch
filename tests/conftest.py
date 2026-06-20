from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def run_tmp_path_tests_from_tmp_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
