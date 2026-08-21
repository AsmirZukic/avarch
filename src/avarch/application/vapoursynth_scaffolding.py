from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class VapourSynthScaffoldError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScriptScaffoldResult:
    destination: Path


def scaffold_filter_script(*, scripts_dir: Path, name: str) -> ScriptScaffoldResult:
    return _write_script(
        scripts_dir=scripts_dir,
        filename=f"{name}.py",
        text=filter_scaffold_text(),
    )


def scaffold_template_script(*, scripts_dir: Path, name: str) -> ScriptScaffoldResult:
    return _write_script(
        scripts_dir=scripts_dir,
        filename=f"{name}.vpy",
        text=template_scaffold_text(),
    )


def filter_scaffold_text() -> str:
    return """from __future__ import annotations

import vapoursynth as vs

from avarch.vpy_api import FilterContext


def apply(video: vs.VideoNode, context: FilterContext) -> vs.VideoNode:
    del context
    return video
"""


def template_scaffold_text() -> str:
    return """from __future__ import annotations

from avarch.vpy_api import bestsource_clip

clip = bestsource_clip()
clip.set_output(index=0)
"""


def _write_script(*, scripts_dir: Path, filename: str, text: str) -> ScriptScaffoldResult:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    destination = scripts_dir / filename
    if destination.exists():
        raise VapourSynthScaffoldError(f"Script already exists: {destination}")
    destination.write_text(text, encoding="utf-8")
    return ScriptScaffoldResult(destination=destination)
