from __future__ import annotations

import re
import tomllib
from pathlib import Path

from avarch.config import WORKSPACE_CONFIG_TEXT


def test_performance_automation_doc_toml_example_parses() -> None:
    root = Path(__file__).resolve().parents[1]
    document = (root / "docs/performance-automation.md").read_text(encoding="utf-8")
    match = re.search(r"```toml\n(?P<toml>.*?)\n```", document, flags=re.DOTALL)

    assert match is not None
    parsed = tomllib.loads(match.group("toml"))
    assert parsed["av1an"]["workers"] == "auto"
    assert parsed["av1an"]["svt_lp"] == "native"
    assert parsed["resources"]["memory_reserve"] == "10%"
    assert parsed["performance"]["calibration_enabled"] is True


def test_default_config_documents_resource_and_performance_policy() -> None:
    parsed = tomllib.loads(WORKSPACE_CONFIG_TEXT)

    assert parsed["resources"]["reservation_allocator"] is True
    assert parsed["resources"]["memory_reserve"] == "10%"
    assert parsed["performance"]["calibration_max_predicted_fraction"] == 0.01
