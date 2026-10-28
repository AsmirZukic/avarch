from __future__ import annotations

from pathlib import Path

import pytest
import typer
from sqlmodel import Session, select
from typer.testing import CliRunner

from avarch.adapters.promotion import PromotionWorkflowAdapter
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.models import Job, PromotionRecord
from avarch.adapters.sqlite.promotion_approvals import current_promotion_approval
from avarch.models.promotion import PromotionMode
from avarch.presentation.commands.promotion import make_promotion_command
from tests.promotion_helpers import fake_replacement_decode, ready_job


@pytest.mark.parametrize("delete_backup", [False, True])
def test_cli_records_replacement_and_separate_backup_deletion_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delete_backup: bool,
) -> None:
    monkeypatch.setattr(
        "avarch.adapters.promotion_service.validate_replacement_media", fake_replacement_decode
    )
    config, job_id, source, _ = ready_job(tmp_path, output_bytes=b"encoded")
    engine = create_db_engine(config.database.url)
    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        job.promotion_approval_json = None
        session.add(job)
    app = typer.Typer()
    app.command()(
        make_promotion_command(
            load_runtime_config=lambda: config, workflow_factory=PromotionWorkflowAdapter
        )
    )
    args = [str(job_id), "--mode", "replace-atomic", "--confirm"]
    if delete_backup:
        args.append("--delete-backup")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert source.read_bytes() == b"encoded"
    assert source.with_name(source.name + ".avarch-original").exists() is not delete_backup
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        approval = current_promotion_approval(job)
        assert approval is not None and approval.mode == PromotionMode.REPLACE_ATOMIC
        assert approval.delete_backup == delete_backup
        assert session.exec(select(PromotionRecord)).one().delete_backup_approved == delete_backup


def test_dry_run_does_not_store_approval_and_cannot_execute_recovery(tmp_path: Path) -> None:
    config, job_id, source, _ = ready_job(tmp_path, output_bytes=b"encoded")
    engine = create_db_engine(config.database.url)
    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        job.promotion_approval_json = None
        session.add(job)
    app = typer.Typer()
    app.command()(
        make_promotion_command(
            load_runtime_config=lambda: config, workflow_factory=PromotionWorkflowAdapter
        )
    )
    runner = CliRunner()
    result = runner.invoke(app, [str(job_id), "--mode", "replace-atomic", "--confirm", "--dry-run"])
    assert result.exit_code == 0
    result = runner.invoke(app, [str(job_id), "--recover", "--dry-run"])
    assert result.exit_code == 1
    assert "cannot be combined" in result.output
    assert source.read_bytes() == b"original"
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job is not None and job.promotion_approval_json is None
        assert session.exec(select(PromotionRecord)).all() == []
