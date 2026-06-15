from pathlib import Path

import pytest

from avarch.models.plan import TranscodePlan, VapourSynthPlan
from avarch.vapoursynth import (
    ResolvedVapourSynthTemplate,
    VapourSynthTemplateError,
    build_template_hash,
    generate_custom_script,
    generate_vapoursynth_script,
    validate_script_syntax,
)
from tests.test_plan_models import sample_plan


def test_custom_script_contains_generated_preamble_and_body() -> None:
    plan, template = _custom_plan_and_template("clip.set_output(index=0)\n")

    script = generate_custom_script(plan, template=template)

    assert "AVARCH_SOURCE_PATH: str = '/media/movie.mkv'" in script
    assert "AVARCH_VIDEO_STREAM_INDEX: int = 0" in script
    assert "AVARCH_TARGET_WIDTH: int = 1920" in script
    assert "AVARCH_PLAN_HASH: str = 'plan-hash'" in script
    assert script.endswith("clip.set_output(index=0)\n")


def test_custom_script_normalizes_line_endings_and_passes_syntax_validation() -> None:
    plan, template = _custom_plan_and_template("clip = 1\r\nclip.set_output(index=0)\r")

    script = generate_custom_script(plan, template=template)

    assert "\r" not in script
    validate_script_syntax(script)


def test_custom_script_rejects_missing_resolved_template() -> None:
    plan, _template = _custom_plan_and_template("clip.set_output(index=0)\n")

    with pytest.raises(VapourSynthTemplateError):
        generate_vapoursynth_script(plan, template=None)


def test_custom_script_rejects_template_hash_mismatch() -> None:
    plan, template = _custom_plan_and_template("clip.set_output(index=0)\n")
    bad_template = ResolvedVapourSynthTemplate(
        path=template.path,
        text=template.text,
        template_hash="bad",
    )

    with pytest.raises(VapourSynthTemplateError):
        generate_custom_script(plan, template=bad_template)


def test_custom_template_can_accept_hdr_source() -> None:
    plan, template = _custom_plan_and_template("clip.set_output(index=0)\n")
    plan = plan.model_copy(
        update={
            "vapoursynth": plan.vapoursynth.model_copy(
                update={
                    "source_color_transfer": "smpte2084",
                    "source_hdr_metadata_present": True,
                }
            )
        }
    )

    assert generate_custom_script(plan, template=template)


def _custom_plan_and_template(body: str) -> tuple[TranscodePlan, ResolvedVapourSynthTemplate]:
    template_hash = build_template_hash(body)
    template_path = Path("/templates/custom.vpy")
    base = sample_plan()
    vapoursynth = VapourSynthPlan(
        generator_version=1,
        mode="custom_template",
        script_path=base.vapoursynth.script_path,
        source_path=base.vapoursynth.source_path,
        source_stream_index=base.vapoursynth.source_stream_index,
        index_cache_dir=base.vapoursynth.index_cache_dir,
        target_width=base.vapoursynth.target_width,
        target_height=base.vapoursynth.target_height,
        source_pix_fmt=base.vapoursynth.source_pix_fmt,
        source_color_transfer=base.vapoursynth.source_color_transfer,
        source_color_primaries=base.vapoursynth.source_color_primaries,
        source_color_space=base.vapoursynth.source_color_space,
        source_hdr_metadata_present=base.vapoursynth.source_hdr_metadata_present,
        hdr_to_sdr=base.vapoursynth.hdr_to_sdr,
        template_path=template_path,
        template_hash=template_hash,
        identity_hash="custom-identity",
    )
    plan = base.model_copy(update={"vapoursynth": vapoursynth})
    template = ResolvedVapourSynthTemplate(
        path=template_path,
        text=body,
        template_hash=template_hash,
    )
    return plan, template
