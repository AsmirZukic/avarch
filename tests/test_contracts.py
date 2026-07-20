from pathlib import Path

from avarch.contracts import (
    ALEMBIC_BASELINE_REVISION,
    ALEMBIC_HEAD_REVISION,
    AV1AN_SPEC_HASH_CONTRACT,
    EXECUTION_ENVIRONMENT_HASH_CONTRACT,
    EXECUTION_IDENTITY_HASH_CONTRACT,
    FFMPEG_MUX_SPEC_HASH_CONTRACT,
    PLAN_HASH_CONTRACT,
    PLAN_SEMANTIC_HASH_CONTRACT,
    PROFILE_HASH_CONTRACT,
    PROMOTION_POLICY_HASH_CONTRACT,
    QUEUE_CONTRACT,
    RESOURCE_POLICY_HASH_CONTRACT,
    VALIDATION_POLICY_HASH_CONTRACT,
    VAPOURSYNTH_IDENTITY_HASH_CONTRACT,
    VAPOURSYNTH_SCRIPT_HASH_CONTRACT,
    VAPOURSYNTH_TEMPLATE_HASH_CONTRACT,
    WORK_KEY_CONTRACT,
    WORKLOAD_SIGNATURE_HASH_CONTRACT,
)


def test_current_hash_contracts_are_single_baseline() -> None:
    assert QUEUE_CONTRACT == "queue-job-v1"
    assert WORK_KEY_CONTRACT == "work-v5"
    assert VAPOURSYNTH_TEMPLATE_HASH_CONTRACT == "vpy-template-v1"
    assert VAPOURSYNTH_SCRIPT_HASH_CONTRACT == "vpy-script-v1"
    assert VAPOURSYNTH_IDENTITY_HASH_CONTRACT == "vpy-identity-v1"
    assert EXECUTION_IDENTITY_HASH_CONTRACT == "execution-identity-v1"
    assert PLAN_HASH_CONTRACT == "plan-v2"
    assert PLAN_SEMANTIC_HASH_CONTRACT == "plan-semantic-v1"
    assert RESOURCE_POLICY_HASH_CONTRACT == "resource-policy-v1"
    assert EXECUTION_ENVIRONMENT_HASH_CONTRACT == "execution-environment-v1"
    assert WORKLOAD_SIGNATURE_HASH_CONTRACT == "workload-signature-v1"
    assert PROFILE_HASH_CONTRACT == "profile-v3"
    assert AV1AN_SPEC_HASH_CONTRACT == "av1an-spec-v1"
    assert FFMPEG_MUX_SPEC_HASH_CONTRACT == "ffmpeg-mux-spec-v1"
    assert VALIDATION_POLICY_HASH_CONTRACT == "validation-policy-v2"
    assert PROMOTION_POLICY_HASH_CONTRACT == "promotion-policy-v1"


def test_current_alembic_revision_chain() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    revisions = sorted((repo_root / "migrations" / "versions").glob("*.py"))

    assert ALEMBIC_BASELINE_REVISION == "0001_initial"
    assert ALEMBIC_HEAD_REVISION == "0014_calibration_observations"
    assert [revision.name for revision in revisions] == [
        "0001_initial_schema.py",
        "0002_media_plan.py",
        "0003_job_outcome_reason.py",
        "0004_promotion_target_lock.py",
        "0005_job_state_version.py",
        "0006_job_attempt_progress.py",
        "0007_structured_attempt_progress.py",
        "0008_scheduler_sessions_and_lifecycle_events.py",
        "0009_scheduler_runtime_capacity.py",
        "0010_performance_observations.py",
        "0011_performance_environment_signature.py",
        "0012_performance_workload_signature.py",
        "0013_resource_reservations.py",
        "0014_calibration_observations.py",
    ]
