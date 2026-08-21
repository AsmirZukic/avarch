from __future__ import annotations

import pytest

from avarch.domain.encoder_args import (
    EncoderArgumentError,
    apply_svt_parallelism,
    parse_encoder_args,
)


def test_svt_lp_rejects_raw_definition() -> None:
    arguments = parse_encoder_args("--preset 6 --lp 4")

    with pytest.raises(EncoderArgumentError, match="must be configured with svt_lp"):
        apply_svt_parallelism(arguments, svt_lp="native")


def test_native_svt_lp_preserves_encoder_arguments() -> None:
    arguments = parse_encoder_args('--preset 6 --metadata "director cut" --crf 28')

    assert apply_svt_parallelism(arguments, svt_lp="native") == [
        "--preset",
        "6",
        "--metadata",
        "director cut",
        "--crf",
        "28",
    ]


def test_svt_lp_appends_structured_definition() -> None:
    arguments = parse_encoder_args("--preset 6 --crf 28")

    assert apply_svt_parallelism(arguments, svt_lp=4) == [
        "--preset",
        "6",
        "--crf",
        "28",
        "--lp",
        "4",
    ]
