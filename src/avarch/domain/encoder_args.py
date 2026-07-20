from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Literal

LP_OPTIONS = frozenset({"--lp", "-lp"})


class EncoderArgumentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SvtLpDefinition:
    value: int
    token_index: int


def parse_encoder_args(value: str) -> list[str]:
    return shlex.split(value)


def svt_lp_definitions(arguments: list[str]) -> list[SvtLpDefinition]:
    definitions: list[SvtLpDefinition] = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        option, separator, inline_value = token.partition("=")
        if option not in LP_OPTIONS:
            index += 1
            continue
        if separator:
            value_text = inline_value
        else:
            next_index = index + 1
            if next_index >= len(arguments):
                raise EncoderArgumentError("--lp requires a value")
            value_text = arguments[next_index]
            index = next_index
        definitions.append(
            SvtLpDefinition(value=_parse_positive_int(value_text, "--lp"), token_index=index)
        )
        index += 1
    return definitions


def normalize_svt_operational_args(
    arguments: list[str],
    *,
    structured_svt_lp: int | Literal["native"] | None = None,
) -> list[str]:
    definitions = svt_lp_definitions(arguments)
    if len(definitions) > 1:
        raise EncoderArgumentError("SVT --lp may only be defined once")
    if structured_svt_lp is not None and definitions:
        raise EncoderArgumentError("SVT --lp is owned by both svt_lp and raw video_args")
    if structured_svt_lp is None or structured_svt_lp == "native":
        return list(arguments)
    return [*arguments, "--lp", str(_parse_positive_int(str(structured_svt_lp), "svt_lp"))]


def _parse_positive_int(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise EncoderArgumentError(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise EncoderArgumentError(f"{label} must be a positive integer")
    return parsed
