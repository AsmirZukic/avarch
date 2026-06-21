from __future__ import annotations

import logging
import sys
from typing import Literal

import structlog

LogFormat = Literal["console", "json"]


def configure_logging(level: str = "INFO", log_format: str = "console") -> None:
    normalized_level = level.upper()
    numeric_level = logging.getLevelNamesMapping().get(normalized_level)

    if numeric_level is None:
        raise ValueError(f"Invalid logging level: {level}")

    if log_format not in {"console", "json"}:
        raise ValueError(f"Invalid logging format: {log_format}")

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer = (
        structlog.dev.ConsoleRenderer()
        if log_format == "console"
        else structlog.processors.JSONRenderer()
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    logging.getLogger("alembic").setLevel(logging.WARNING)

    for handler in root_logger.handlers:
        handler.setFormatter(formatter)
        handler.setLevel(numeric_level)
