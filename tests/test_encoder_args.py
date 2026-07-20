from __future__ import annotations

import pytest

from avarch.domain.encoder_args import (
    EncoderArgumentError,
    normalize_svt_operational_args,
    parse_encoder_args,
    svt_lp_definitions,
)


def test_svt_lp_definitions_recognize_split_and_equals_forms() -> None:
    arguments = parse_encoder_args("--preset 6 --lp 4 --crf 28")
    assert [definition.value for definition in svt_lp_definitions(arguments)] == [4]

    arguments = parse_encoder_args("--preset 6 --lp=8 --crf 28")
    assert [definition.value for definition in svt_lp_definitions(arguments)] == [8]


def test_svt_lp_rejects_repeated_raw_definitions() -> None:
    arguments = parse_encoder_args("--lp 4 --crf 28 --lp=8")

    with pytest.raises(EncoderArgumentError, match="may only be defined once"):
        normalize_svt_operational_args(arguments)


def test_svt_lp_rejects_structured_and_raw_ownership() -> None:
    arguments = parse_encoder_args("--preset 6 --lp 4")

    with pytest.raises(EncoderArgumentError, match="both svt_lp and raw video_args"):
        normalize_svt_operational_args(arguments, structured_svt_lp=4)


def test_svt_lp_preserves_unrelated_raw_arguments() -> None:
    arguments = parse_encoder_args('--preset 6 --metadata "director cut" --crf 28')

    assert normalize_svt_operational_args(arguments) == [
        "--preset",
        "6",
        "--metadata",
        "director cut",
        "--crf",
        "28",
    ]


def test_svt_lp_appends_one_structured_definition() -> None:
    arguments = parse_encoder_args("--preset 6 --crf 28")

    assert normalize_svt_operational_args(arguments, structured_svt_lp=4) == [
        "--preset",
        "6",
        "--crf",
        "28",
        "--lp",
        "4",
    ]
