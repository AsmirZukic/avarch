from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from typing import Protocol

from avarch.config import AppConfig

MAX_CLI_LOG_TAIL_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class SchedulerRunSummary:
    completed: int
    failed: int
    skipped: int
    idle: bool


class SchedulerRunner(Protocol):
    async def run_scheduler(
        self,
        *,
        config: AppConfig,
        runner_id: str,
        resume: bool,
    ) -> SchedulerRunSummary: ...


def new_runner_id() -> str:
    return uuid.uuid4().hex


def cli_actor() -> str:
    return f"cli:{socket.gethostname()}:{os.getpid()}"


async def run_scheduler(
    runner: SchedulerRunner,
    *,
    config: AppConfig,
    runner_id: str,
    resume: bool = False,
) -> SchedulerRunSummary:
    return await runner.run_scheduler(config=config, runner_id=runner_id, resume=resume)
