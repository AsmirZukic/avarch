from __future__ import annotations

import pytest

from avarch.domain.planning import TargetDimensionError, calculate_target_dimensions


def test_4k_scales_to_1920x1080() -> None:
    dimensions = calculate_target_dimensions(
        source_width=3840,
        source_height=2160,
        max_width=1920,
    )

    assert dimensions.width == 1920
    assert dimensions.height == 1080
    assert dimensions.resize_required is True


def test_ultrawide_source_preserves_aspect_ratio() -> None:
    dimensions = calculate_target_dimensions(
        source_width=2560,
        source_height=1080,
        max_width=1920,
    )

    assert dimensions.width == 1920
    assert dimensions.height == 810


def test_1080p_source_is_unchanged() -> None:
    dimensions = calculate_target_dimensions(
        source_width=1920,
        source_height=1080,
        max_width=1920,
    )

    assert dimensions.width == 1920
    assert dimensions.height == 1080
    assert dimensions.resize_required is False


def test_odd_dimensions_are_normalized_to_even_values() -> None:
    dimensions = calculate_target_dimensions(
        source_width=1919,
        source_height=1079,
        max_width=1920,
    )

    assert dimensions.width == 1918
    assert dimensions.height == 1078
    assert dimensions.resize_required is True


def test_invalid_dimensions_are_rejected() -> None:
    with pytest.raises(TargetDimensionError):
        calculate_target_dimensions(source_width=0, source_height=1080, max_width=1920)

    with pytest.raises(TargetDimensionError):
        calculate_target_dimensions(source_width=1920, source_height=0, max_width=1920)

    with pytest.raises(TargetDimensionError):
        calculate_target_dimensions(source_width=1920, source_height=1080, max_width=1919)