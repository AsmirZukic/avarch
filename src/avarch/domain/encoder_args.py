from __future__ import annotations

import shlex
from typing import Literal

SVT_PARALLELISM_OPTIONS = frozenset({"--lp", "-lp"})


class EncoderArgumentError(ValueError):
    pass


def parse_encoder_args(value: str) -> list[str]:
    return shlex.split(value)


def apply_svt_parallelism(
    arguments: list[str],
    *,
    svt_lp: int | Literal["native"],
) -> list[str]:
    for token in arguments:
        option, _separator, _value = token.partition("=")
        if option in SVT_PARALLELISM_OPTIONS:
            raise EncoderArgumentError("SVT --lp must be configured with svt_lp, not video_args")
    if svt_lp == "native":
        return list(arguments)
    if isinstance(svt_lp, bool) or svt_lp <= 0:
        raise EncoderArgumentError("svt_lp must be positive or 'native'")
    return [*arguments, "--lp", str(svt_lp)]
