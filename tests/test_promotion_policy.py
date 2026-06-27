from avarch.application.planning import build_promotion_policy_hash, finalize_promotion_policy
from avarch.models.promotion import PromotionMode, PromotionPolicy


def test_policy_allows_three_promotion_modes() -> None:
    policy = finalize_promotion_policy(PromotionPolicy(policy_hash=""))

    assert policy.allowed_modes == (
        PromotionMode.KEEP_ORIGINAL.value,
        PromotionMode.MOVE_ORIGINAL_TO_BACKUP.value,
        PromotionMode.REPLACE_ATOMIC.value,
    )


def test_promotion_suffixes_are_deterministic() -> None:
    policy = finalize_promotion_policy(PromotionPolicy(policy_hash=""))

    assert policy.keep_original_name_suffix == ".av1"
    assert policy.backup_name_suffix == ".avarch-original"


def test_policy_hash_is_deterministic_and_excludes_itself() -> None:
    policy = finalize_promotion_policy(PromotionPolicy(policy_hash=""))
    changed_self = policy.model_copy(update={"policy_hash": "different"})

    assert policy.policy_hash == build_promotion_policy_hash(changed_self)
