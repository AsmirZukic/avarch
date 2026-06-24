from pathlib import Path

from avarch.domain.promotion import (
    derive_backup_path,
    derive_keep_original_path,
    derive_promotion_journal_path,
    derive_staging_path,
)


def test_keep_original_mkv_path() -> None:
    assert derive_keep_original_path(Path("/media/Movie.mkv"), container="mkv") == Path(
        "/media/Movie.av1.mkv"
    )


def test_keep_original_replaces_source_extension() -> None:
    assert derive_keep_original_path(Path("/media/Movie.mp4"), container="mkv") == Path(
        "/media/Movie.av1.mkv"
    )


def test_multi_dot_filename_is_preserved() -> None:
    assert derive_keep_original_path(Path("/media/Episode.01.mkv"), container="mkv") == Path(
        "/media/Episode.01.av1.mkv"
    )


def test_backup_suffix_is_appended_to_full_name() -> None:
    assert derive_backup_path(Path("/media/Movie.mkv")) == Path("/media/Movie.mkv.avarch-original")


def test_staging_is_hidden_sibling_of_final() -> None:
    staging = derive_staging_path(Path("/media/Movie.av1.mkv"), operation_id="abcdef123456")

    assert staging == Path("/media/.Movie.av1.mkv.avarch-promote-abcdef123456.tmp")


def test_staging_uses_operation_id() -> None:
    first = derive_staging_path(Path("/media/Movie.av1.mkv"), operation_id="111111")
    second = derive_staging_path(Path("/media/Movie.av1.mkv"), operation_id="222222")

    assert first != second


def test_journal_is_under_runtime_directory() -> None:
    assert derive_promotion_journal_path(Path("/work/runtime")) == Path(
        "/work/runtime/promotion-journal.json"
    )
