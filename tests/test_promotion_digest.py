from pathlib import Path

from avarch.adapters.filesystem.promotion import calculate_promotion_digest


def test_digest_is_deterministic(tmp_path: Path) -> None:
    path = tmp_path / "output.mkv"
    path.write_bytes(b"content")

    assert calculate_promotion_digest(path) == calculate_promotion_digest(path)


def test_digest_changes_with_content(tmp_path: Path) -> None:
    path = tmp_path / "output.mkv"
    path.write_bytes(b"content")
    first = calculate_promotion_digest(path)

    path.write_bytes(b"different")

    assert calculate_promotion_digest(path) != first


def test_digest_prefix_separates_contract(tmp_path: Path) -> None:
    path = tmp_path / "output.mkv"
    path.write_bytes(b"")

    assert calculate_promotion_digest(path) != calculate_promotion_digest.__name__
