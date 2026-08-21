from __future__ import annotations

from dataclasses import dataclass


class TargetDimensionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TargetDimensions:
    width: int
    height: int
    resize_required: bool


def calculate_target_dimensions(
    *,
    source_width: int,
    source_height: int,
    max_width: int,
) -> TargetDimensions:
    if source_width <= 0:
        raise TargetDimensionError("Source width must be positive.")
    if source_height <= 0:
        raise TargetDimensionError("Source height must be positive.")
    if max_width <= 0:
        raise TargetDimensionError("Profile max_width must be positive.")
    if max_width % 2 != 0:
        raise TargetDimensionError("Profile max_width must be even.")

    candidate_width = min(source_width, max_width)
    target_width = candidate_width - candidate_width % 2
    if target_width < 2:
        raise TargetDimensionError("Target width must be at least 2.")

    if target_width == source_width:
        target_height = source_height - source_height % 2
    else:
        numerator = source_height * target_width
        target_height = ((numerator + source_width) // (2 * source_width)) * 2

    if target_height < 2:
        raise TargetDimensionError("Target height must be at least 2.")

    return TargetDimensions(
        width=target_width,
        height=target_height,
        resize_required=target_width != source_width or target_height != source_height,
    )
