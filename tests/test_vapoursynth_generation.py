from pathlib import Path

import pytest

from avarch.vapoursynth import (
    HdrProcessingNotImplementedError,
    UnsupportedSourceFormatError,
    generate_builtin_script,
    validate_script_syntax,
)
from tests.test_plan_models import sample_plan


def test_builtin_script_is_deterministic() -> None:
    plan = sample_plan()

    assert generate_builtin_script(plan) == generate_builtin_script(plan)


def test_builtin_script_uses_finalized_plan_hash() -> None:
    script = generate_builtin_script(sample_plan())

    assert "# Plan hash: plan-hash" in script


def test_builtin_script_uses_bestsource_loader() -> None:
    script = generate_builtin_script(sample_plan())

    assert "clip = core.bs.VideoSource(source=source_path)" in script
    assert "core.lsmas" not in script
    assert "LWLibavSource" not in script


def test_builtin_script_uses_planned_dimensions_and_output_format() -> None:
    script = generate_builtin_script(sample_plan())

    assert "width=1920" in script
    assert "height=1080" in script
    assert "format=vs.YUV420P10" in script
    assert "clip.set_output(index=0)" in script


def test_builtin_script_ends_with_one_newline_and_passes_static_validation() -> None:
    script = generate_builtin_script(sample_plan())

    assert script.endswith("\n")
    assert not script.endswith("\n\n")
    validate_script_syntax(script)


def test_builtin_script_escapes_path_as_python_literal() -> None:
    plan = sample_plan().model_copy(
        update={
            "input_path": Path("/media/movie's.mkv"),
            "vapoursynth": sample_plan().vapoursynth.model_copy(
                update={"source_path": Path("/media/movie's.mkv")}
            ),
        }
    )

    script = generate_builtin_script(plan)

    assert 'source_path = "/media/movie\'s.mkv"' in script
    validate_script_syntax(script)


def test_builtin_script_rejects_unsupported_pixel_format() -> None:
    plan = sample_plan().model_copy(
        update={
            "vapoursynth": sample_plan().vapoursynth.model_copy(
                update={"source_pix_fmt": "yuv444p10le"}
            )
        }
    )

    with pytest.raises(UnsupportedSourceFormatError):
        generate_builtin_script(plan)


def test_builtin_script_rejects_hdr_source() -> None:
    plan = sample_plan().model_copy(
        update={
            "vapoursynth": sample_plan().vapoursynth.model_copy(
                update={
                    "source_color_transfer": "smpte2084",
                    "source_hdr_metadata_present": True,
                }
            )
        }
    )

    with pytest.raises(HdrProcessingNotImplementedError):
        generate_builtin_script(plan)
