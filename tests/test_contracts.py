from pathlib import Path

from avarch.contracts import (
    ALEMBIC_BASELINE_REVISION,
    AV1AN_COMMAND_SCHEMA_VERSION,
    AV1AN_SPEC_HASH_CONTRACT,
    AV1AN_STAGE_MARKER_SCHEMA_VERSION,
    ENCODE_RESULT_RECEIPT_SCHEMA_VERSION,
    EXECUTION_IDENTITY_HASH_CONTRACT,
    EXECUTION_IDENTITY_SCHEMA_VERSION,
    FFMPEG_MUX_SCHEMA_VERSION,
    FFMPEG_MUX_SPEC_HASH_CONTRACT,
    NORMALIZED_PROBE_SCHEMA_VERSION,
    PLAN_HASH_CONTRACT,
    PROFILE_HASH_CONTRACT,
    PROMOTION_POLICY_HASH_CONTRACT,
    QUEUE_CONTRACT,
    TRANSCODE_PLAN_SCHEMA_VERSION,
    VALIDATION_POLICY_HASH_CONTRACT,
    VALIDATION_POLICY_SCHEMA_VERSION,
    VALIDATION_REPORT_SCHEMA_VERSION,
    VAPOURSYNTH_IDENTITY_HASH_CONTRACT,
    VAPOURSYNTH_PLAN_SCHEMA_VERSION,
    VAPOURSYNTH_TEMPLATE_HASH_CONTRACT,
    WORK_KEY_CONTRACT,
)


def test_current_schema_versions_are_single_baseline() -> None:
    assert TRANSCODE_PLAN_SCHEMA_VERSION == 5
    assert NORMALIZED_PROBE_SCHEMA_VERSION == 1
    assert VALIDATION_POLICY_SCHEMA_VERSION == 2
    assert VALIDATION_REPORT_SCHEMA_VERSION == 1
    assert VAPOURSYNTH_PLAN_SCHEMA_VERSION == 1
    assert AV1AN_COMMAND_SCHEMA_VERSION == 2
    assert FFMPEG_MUX_SCHEMA_VERSION == 1
    assert EXECUTION_IDENTITY_SCHEMA_VERSION == 1
    assert AV1AN_STAGE_MARKER_SCHEMA_VERSION == 1
    assert ENCODE_RESULT_RECEIPT_SCHEMA_VERSION == 1


def test_current_hash_contracts_are_single_baseline() -> None:
    assert QUEUE_CONTRACT == "queue-job-v1"
    assert WORK_KEY_CONTRACT == "work-v5"
    assert VAPOURSYNTH_TEMPLATE_HASH_CONTRACT == "vpy-template-v1"
    assert VAPOURSYNTH_IDENTITY_HASH_CONTRACT == "vpy-identity-v1"
    assert EXECUTION_IDENTITY_HASH_CONTRACT == "execution-identity-v1"
    assert PLAN_HASH_CONTRACT == "plan-v2"
    assert PROFILE_HASH_CONTRACT == "profile-v3"
    assert AV1AN_SPEC_HASH_CONTRACT == "av1an-spec-v1"
    assert FFMPEG_MUX_SPEC_HASH_CONTRACT == "ffmpeg-mux-spec-v1"
    assert VALIDATION_POLICY_HASH_CONTRACT == "validation-policy-v2"
    assert PROMOTION_POLICY_HASH_CONTRACT == "promotion-policy-v1"


def test_current_alembic_revision_is_single_baseline() -> None:
    revisions = list(Path("migrations/versions").glob("*.py"))

    assert ALEMBIC_BASELINE_REVISION == "0001_initial"
    assert [revision.name for revision in revisions] == ["0001_initial_schema.py"]
