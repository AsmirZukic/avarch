import json
import logging

import pytest
import structlog
from pytest import CaptureFixture

from avarch.logging import configure_logging


def test_configure_logging_sets_root_level() -> None:
    configure_logging(level="DEBUG", log_format="console")

    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_does_not_duplicate_handlers() -> None:
    configure_logging(level="INFO", log_format="console")
    first_count = len(logging.getLogger().handlers)

    configure_logging(level="INFO", log_format="console")
    second_count = len(logging.getLogger().handlers)

    assert second_count == first_count


def test_json_logging_outputs_structured_fields(capsys: CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format="json")

    log = structlog.get_logger("test")
    log.info("job_created", job_id=123, profile_name="av1_1080p_sdr")

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["event"] == "job_created"
    assert payload["job_id"] == 123
    assert payload["profile_name"] == "av1_1080p_sdr"
    assert payload["level"] == "info"


def test_invalid_log_format_fails() -> None:
    with pytest.raises(ValueError):
        configure_logging(level="INFO", log_format="xml")


def test_invalid_log_level_fails() -> None:
    with pytest.raises(ValueError):
        configure_logging(level="TRACE", log_format="console")
