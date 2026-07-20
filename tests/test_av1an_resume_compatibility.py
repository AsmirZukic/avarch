from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.encoder_integration


def _require_resume_compatibility_suite() -> None:
    if os.environ.get("AVARCH_RUN_ENCODER_INTEGRATION") != "1":
        pytest.skip("set AVARCH_RUN_ENCODER_INTEGRATION=1 to run real encoder checks")


def test_av1an_resume_compatibility_for_worker_count_change() -> None:
    _require_resume_compatibility_suite()
    pytest.fail("real worker-count resume compatibility fixture is not configured")


def test_av1an_resume_compatibility_for_svt_lp_change() -> None:
    _require_resume_compatibility_suite()
    pytest.fail("real svt --lp resume compatibility fixture is not configured")
