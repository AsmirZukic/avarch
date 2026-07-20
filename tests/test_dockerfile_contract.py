from __future__ import annotations

from pathlib import Path


def test_dockerfile_pins_av1an_to_exact_version() -> None:
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")

    assert 'ARG AVARCH_AV1AN_VERSION=">=0.5,<0.6"' not in dockerfile
    assert "ARG AVARCH_AV1AN_VERSION=0.5.1" in dockerfile
    assert 'cargo install av1an --version "=${AVARCH_AV1AN_VERSION}" --locked' in dockerfile
