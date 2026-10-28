"""Pure recovery decisions; observations must be verified by filesystem adapters."""

from enum import StrEnum


class InstallDecision(StrEnum):
    INSTALL = "install"
    VERIFY = "verify"
    REQUIRE_RECOVERY = "require_recovery"


def decide_install(
    *,
    phase: str,
    installed_owned: bool,
    staged_owned: bool,
    source_unchanged: bool,
    destination_exists: bool,
    keep_original: bool,
) -> InstallDecision:
    if phase in {"install_pending", "final_installed", "verified"} and installed_owned:
        return InstallDecision.VERIFY
    if phase in {"final_installed", "verified"}:
        return InstallDecision.REQUIRE_RECOVERY
    if not staged_owned or not source_unchanged:
        return InstallDecision.REQUIRE_RECOVERY
    if keep_original and destination_exists:
        return InstallDecision.REQUIRE_RECOVERY
    return InstallDecision.INSTALL
