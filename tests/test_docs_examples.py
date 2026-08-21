from __future__ import annotations

import tomllib

from avarch.config import WORKSPACE_CONFIG_TEXT


def test_default_config_documents_scheduler_concurrency() -> None:
    parsed = tomllib.loads(WORKSPACE_CONFIG_TEXT)

    assert parsed["resources"] == {
        "cheap_workers": 4,
        "av1an_jobs": 1,
        "file_ops": 1,
    }
