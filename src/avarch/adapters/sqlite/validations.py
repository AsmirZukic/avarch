from __future__ import annotations

from collections.abc import Sequence

from sqlmodel import Session

from avarch.adapters.sqlite.models import Job, JobAttempt, ValidationResult
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus
from avarch.models.validation import ValidationReport
from avarch.serialization import canonical_json


class ValidationPersistenceError(RuntimeError):
    pass


def latest_validation(session: Session, job: Job) -> ValidationResult | None:
    if job.latest_validation_id is None:
        return None
    result = session.get(ValidationResult, job.latest_validation_id)
    if result is None or result.job_id != job.id:
        return None
    if result.plan_hash != job.plan_hash or result.output_path != job.output_path:
        return None
    return result


def latest_validation_passed(session: Session, job: Job) -> bool:
    result = latest_validation(session, job)
    return result is not None and result.passed


def persist_validation_result(
    session: Session,
    *,
    job: Job,
    attempt: JobAttempt,
    report: ValidationReport,
    failed_checks: Sequence[str],
    failed_summary: str,
) -> ValidationResult:
    if job.id is None or attempt.id is None:
        raise ValidationPersistenceError("Job and attempt must be persisted.")
    now = report.finished_at
    result = ValidationResult(
        job_id=job.id,
        attempt_id=attempt.id,
        plan_hash=report.plan_hash,
        policy_hash=report.policy_hash,
        output_path=str(report.output_path),
        output_fs_fingerprint=report.output_fs_fingerprint_after,
        passed=report.passed,
        details_json=canonical_json(report),
        created_at=now,
    )
    session.add(result)
    session.flush()
    if result.id is None:
        raise ValidationPersistenceError("Validation result id was not assigned.")

    job.latest_validation_id = result.id
    job.claimed_by = None
    job.updated_at = now
    job.finished_at = None
    attempt.status = AttemptStatus.COMPLETED
    attempt.finished_at = now
    attempt.output_path = str(report.output_path)
    attempt.details_json = canonical_json(
        {
            "result_id": result.id,
            "report_path": str(report.output_path),
            "plan_hash": report.plan_hash,
            "policy_hash": report.policy_hash,
            "failed_checks": list(failed_checks),
        }
    )
    if report.passed:
        job.status = JobStatus.VALIDATED
        job.stage = JobStage.PROMOTE
        job.last_error_type = None
        job.last_error_message = None
    else:
        job.status = JobStatus.FAILED
        job.stage = JobStage.VALIDATE
        job.finished_at = now
        job.last_error_type = "ValidationFailed"
        job.last_error_message = failed_summary
    session.add(job)
    session.add(attempt)
    return result
