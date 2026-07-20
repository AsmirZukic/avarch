from __future__ import annotations

import pytest

from avarch.domain.resource_policy import (
    ResourceMode,
    ResourcePolicyError,
    parse_resource_intent,
)


def test_parse_auto_resource_intent() -> None:
    intent = parse_resource_intent(workers="auto")

    assert intent.mode is ResourceMode.AUTO
    assert intent.workers.value == "auto"
    assert intent.svt_lp.value == "native"


def test_parse_native_resource_intent_forbids_manual_values() -> None:
    with pytest.raises(ResourcePolicyError, match="native resource mode"):
        parse_resource_intent(mode="native", workers=2)

    with pytest.raises(ResourcePolicyError, match="native resource mode"):
        parse_resource_intent(mode="native", workers="auto", svt_lp=4)


def test_parse_manual_resource_intent_requires_manual_workers() -> None:
    with pytest.raises(ResourcePolicyError, match="manual resource mode"):
        parse_resource_intent(mode="manual", workers="auto")

    intent = parse_resource_intent(mode="manual", workers=2, svt_lp=4)

    assert intent.mode is ResourceMode.MANUAL
    assert intent.workers.value == 2
    assert intent.svt_lp.value == 4


def test_parse_auto_resource_intent_rejects_manual_ownership() -> None:
    with pytest.raises(ResourcePolicyError, match="auto resource mode"):
        parse_resource_intent(mode="auto", workers=2)

    with pytest.raises(ResourcePolicyError, match="auto resource mode"):
        parse_resource_intent(mode="auto", workers="auto", svt_lp=4)
