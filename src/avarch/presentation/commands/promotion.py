"""Promotion CLI area. Dependencies are supplied by the CLI composition root."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Protocol

import typer

from avarch.application.promotion import (
    PromotionWorkflow,
    PromotionWorkflowError,
    execute_promotion,
    promotion_preflight,
    recover_promotion,
)
from avarch.application.scheduler_run import new_runner_id
from avarch.cli_rendering import echo_promotion_complete, echo_promotion_preview
from avarch.config import AppConfig
from avarch.models.promotion import PromotionMode


class PromotionCommand(Protocol):
    def __call__(
        self,
        job_id: int,
        mode: PromotionMode = PromotionMode.KEEP_ORIGINAL,
        dry_run: bool = False,
        confirm: bool = False,
        recover: bool = False,
        delete_backup: bool = False,
    ) -> None: ...


def make_promotion_command(
    *,
    load_runtime_config: Callable[[], AppConfig],
    workflow_factory: Callable[[], PromotionWorkflow],
) -> PromotionCommand:
    def promote_job(
        job_id: Annotated[int, typer.Argument(help="Validated job id to promote.")],
        mode: Annotated[
            PromotionMode,
            typer.Option("--mode", help="Promotion filesystem mode."),
        ] = PromotionMode.KEEP_ORIGINAL,
        dry_run: Annotated[
            bool,
            typer.Option(
                "--dry-run",
                help="Preview promotion without changing files or database rows.",
            ),
        ] = False,
        confirm: Annotated[
            bool,
            typer.Option("--confirm", help="Execute the promotion transaction."),
        ] = False,
        recover: Annotated[
            bool,
            typer.Option("--recover", help="Recover an interrupted promotion for this job."),
        ] = False,
        delete_backup: Annotated[
            bool,
            typer.Option(
                "--delete-backup", help="Also approve permanent deletion of the original backup."
            ),
        ] = False,
    ) -> None:
        runtime_config = load_runtime_config()
        owner_token = new_runner_id()

        if recover and dry_run:
            typer.echo("--recover cannot be combined with --dry-run.")
            raise typer.Exit(1)

        if recover:
            try:
                record = asyncio.run(
                    recover_promotion(
                        workflow_factory(),
                        job_id=job_id,
                        config=runtime_config,
                        owner_token=owner_token,
                    )
                )
            except PromotionWorkflowError as exc:
                typer.echo(str(exc))
                raise typer.Exit(1) from exc
            echo_promotion_complete(record)
            return

        try:
            preflight = promotion_preflight(
                workflow_factory(),
                job_id=job_id,
                mode=mode,
                config=runtime_config,
                operation_id=new_runner_id(),
            )
        except PromotionWorkflowError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1) from exc

        if dry_run or not confirm:
            echo_promotion_preview(preflight, dry_run=dry_run)
            return

        try:
            workflow_factory().approve(
                job_ids=[job_id],
                mode=mode,
                config=runtime_config,
                actor=owner_token,
                delete_backup=delete_backup,
            )
            record = asyncio.run(
                execute_promotion(
                    workflow_factory(),
                    job_id=job_id,
                    mode=mode,
                    config=runtime_config,
                    owner_token=owner_token,
                )
            )
        except PromotionWorkflowError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1) from exc

        echo_promotion_complete(record)

    return promote_job
