from __future__ import annotations

from avarch.domain.jobs import JobStage, JobStatus, ResourceClass
from avarch.domain.scheduler import (
    ActiveJob,
    ClaimableJob,
    QueueClearAction,
    ResourceCapacity,
    RetryFacts,
    SchedulerMode,
    classify_queue_clear_job,
    jobs_to_cancel,
    resource_for_stage,
    scheduler_can_launch_jobs,
    select_launchable_jobs,
    select_retry_stage,
)


def test_scheduler_cancel_decision_cancels_requested_active_jobs() -> None:
    active = [
        ActiveJob(job_id=1, stage=JobStage.ENCODE),
        ActiveJob(job_id=2, stage=JobStage.VALIDATE),
    ]

    assert jobs_to_cancel(
        active,
        cancel_requested_job_ids={2, 3},
        mode=SchedulerMode.RUNNING,
    ) == frozenset({2})


def test_scheduler_stop_cancels_all_active_jobs() -> None:
    active = [
        ActiveJob(job_id=1, stage=JobStage.ENCODE),
        ActiveJob(job_id=2, stage=JobStage.VALIDATE),
    ]

    assert jobs_to_cancel(
        active,
        cancel_requested_job_ids=set(),
        mode=SchedulerMode.STOPPING,
    ) == frozenset({1, 2})


def test_scheduler_launches_only_in_running_mode() -> None:
    assert scheduler_can_launch_jobs(SchedulerMode.RUNNING) is True
    assert scheduler_can_launch_jobs(SchedulerMode.PAUSED) is False
    assert scheduler_can_launch_jobs(SchedulerMode.DRAINING) is False
    assert scheduler_can_launch_jobs(SchedulerMode.STOPPING) is False


def test_select_launchable_jobs_accounts_for_active_and_newly_selected_jobs() -> None:
    claimable = [
        ClaimableJob(job_id=1, stage=JobStage.VALIDATE),
        ClaimableJob(job_id=2, stage=JobStage.PROBE),
        ClaimableJob(job_id=3, stage=JobStage.ENCODE),
        ClaimableJob(job_id=4, stage=JobStage.PROMOTE),
    ]

    selected = select_launchable_jobs(
        claimable,
        active_jobs=[ActiveJob(job_id=10, stage=JobStage.ENCODE)],
        capacity=ResourceCapacity(cheap_workers=1, av1an_jobs=1, file_ops=1),
    )

    assert selected == (
        ClaimableJob(job_id=1, stage=JobStage.VALIDATE),
        ClaimableJob(job_id=4, stage=JobStage.PROMOTE),
    )


def test_cleanup_uses_file_operation_capacity() -> None:
    assert resource_for_stage(JobStage.CLEANUP) == ResourceClass.FILE_OP


def test_classify_queue_clear_job_excludes_terminal_and_running_promotion() -> None:
    assert (
        classify_queue_clear_job(
            JobStatus.PROMOTED,
            JobStage.PROMOTE,
            cancel_running=True,
        )
        == QueueClearAction.EXCLUDE_TERMINAL
    )
    assert (
        classify_queue_clear_job(
            JobStatus.ENCODING,
            JobStage.PROMOTE,
            cancel_running=True,
        )
        == QueueClearAction.EXCLUDE_RUNNING_PROMOTION
    )


def test_classify_queue_clear_job_handles_running_cancel_flag() -> None:
    assert (
        classify_queue_clear_job(
            JobStatus.ENCODING,
            JobStage.ENCODE,
            cancel_running=False,
        )
        == QueueClearAction.SKIP_RUNNING
    )
    assert (
        classify_queue_clear_job(
            JobStatus.ENCODING,
            JobStage.ENCODE,
            cancel_running=True,
        )
        == QueueClearAction.REQUEST_RUNNING_CANCEL
    )


def test_classify_queue_clear_job_cancels_nonrunning_immediately() -> None:
    assert (
        classify_queue_clear_job(
            JobStatus.QUEUED,
            JobStage.ENCODE,
            cancel_running=False,
        )
        == QueueClearAction.CANCEL_IMMEDIATE
    )


def test_select_retry_stage_rebuilds_stale_dependencies_first() -> None:
    assert (
        select_retry_stage(
            RetryFacts(
                canonical_probe_matches=False,
                plan_artifact_valid=True,
                plan_identity_matches=True,
                output_exists=True,
                validation_passed=True,
                promotion_completed=False,
            )
        )
        == JobStage.PROBE
    )
    assert (
        select_retry_stage(
            RetryFacts(
                canonical_probe_matches=True,
                plan_artifact_valid=False,
                plan_identity_matches=True,
                output_exists=True,
                validation_passed=True,
                promotion_completed=False,
            )
        )
        == JobStage.PLAN
    )


def test_select_retry_stage_resumes_from_candidate_state() -> None:
    assert (
        select_retry_stage(
            RetryFacts(
                canonical_probe_matches=True,
                plan_artifact_valid=True,
                plan_identity_matches=True,
                output_exists=False,
                validation_passed=False,
                promotion_completed=False,
            )
        )
        == JobStage.ENCODE
    )
    assert (
        select_retry_stage(
            RetryFacts(
                canonical_probe_matches=True,
                plan_artifact_valid=True,
                plan_identity_matches=True,
                output_exists=True,
                validation_passed=False,
                promotion_completed=False,
            )
        )
        == JobStage.VALIDATE
    )
    assert (
        select_retry_stage(
            RetryFacts(
                canonical_probe_matches=True,
                plan_artifact_valid=True,
                plan_identity_matches=True,
                output_exists=True,
                validation_passed=True,
                promotion_completed=False,
            )
        )
        == JobStage.PROMOTE
    )


def test_select_retry_stage_returns_none_for_completed_promotion() -> None:
    assert (
        select_retry_stage(
            RetryFacts(
                canonical_probe_matches=True,
                plan_artifact_valid=True,
                plan_identity_matches=True,
                output_exists=True,
                validation_passed=True,
                promotion_completed=True,
            )
        )
        is None
    )
