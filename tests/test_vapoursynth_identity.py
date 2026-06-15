import pytest

from avarch.vapoursynth import (
    build_template_hash,
    build_vapoursynth_identity_hash,
    normalize_template_text,
)


def test_template_hash_is_deterministic() -> None:
    text = "clip.set_output(index=0)\n"

    assert build_template_hash(text) == build_template_hash(text)


def test_template_hash_normalizes_line_endings() -> None:
    assert build_template_hash("a\r\nb\rc\n") == build_template_hash("a\nb\nc\n")
    assert normalize_template_text("a\r\nb\rc") == "a\nb\nc"


def test_template_hash_changes_with_content() -> None:
    assert build_template_hash("a\n") != build_template_hash("b\n")


def test_identity_hash_changes_with_generator_version() -> None:
    first = build_vapoursynth_identity_hash(
        generator_version=1,
        mode="generated",
        output_format="YUV420P10",
        resize_filter="spline36",
        template_hash=None,
    )
    second = build_vapoursynth_identity_hash(
        generator_version=2,
        mode="generated",
        output_format="YUV420P10",
        resize_filter="spline36",
        template_hash=None,
    )

    assert first != second


def test_identity_hash_changes_with_template_hash() -> None:
    first = build_vapoursynth_identity_hash(
        generator_version=1,
        mode="custom_template",
        output_format="YUV420P10",
        resize_filter="spline36",
        template_hash="first",
    )
    second = build_vapoursynth_identity_hash(
        generator_version=1,
        mode="custom_template",
        output_format="YUV420P10",
        resize_filter="spline36",
        template_hash="second",
    )

    assert first != second


def test_identity_hash_rejects_template_hash_for_generated_mode() -> None:
    with pytest.raises(ValueError):
        build_vapoursynth_identity_hash(
            generator_version=1,
            mode="generated",
            output_format="YUV420P10",
            resize_filter="spline36",
            template_hash="template",
        )


def test_identity_hash_requires_template_hash_for_custom_mode() -> None:
    with pytest.raises(ValueError):
        build_vapoursynth_identity_hash(
            generator_version=1,
            mode="custom_template",
            output_format="YUV420P10",
            resize_filter="spline36",
            template_hash=None,
        )
