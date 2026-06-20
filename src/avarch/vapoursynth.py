from __future__ import annotations

import ast
import hashlib
import importlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from avarch.contracts import (
    VAPOURSYNTH_IDENTITY_HASH_CONTRACT,
    VAPOURSYNTH_SCRIPT_HASH_CONTRACT,
    VAPOURSYNTH_TEMPLATE_HASH_CONTRACT,
)
from avarch.models.plan import (
    TranscodePlan,
    VapourSynthMode,
    VapourSynthOutputFormat,
    VapourSynthResizeFilter,
)
from avarch.profiles.models import EncodingProfile
from avarch.serialization import canonical_json
from avarch.workspace import WorkspaceContext

GENERATOR_VERSION = 3
DEFAULT_VSPIPE_TIMEOUT_SECONDS = 600.0
MAX_VSPIPE_EXCERPT_BYTES = 8192
_SUPPORTED_PIXEL_FORMATS = {
    "yuv420p",
    "yuvj420p",
    "yuv420p10le",
    "yuv420p10be",
}
_SUPPORTED_SOURCE_CODECS = {"h264", "hevc"}
HDR_TRANSFER_VALUES = {
    "smpte2084",
    "arib-std-b67",
}


class VapourSynthGenerationError(RuntimeError):
    pass


class UnsupportedSourceFormatError(VapourSynthGenerationError):
    pass


class HdrProcessingNotImplementedError(VapourSynthGenerationError):
    pass


class VapourSynthTemplateError(VapourSynthGenerationError):
    pass


class VapourSynthSyntaxError(VapourSynthGenerationError):
    pass


class VapourSynthScriptPathError(VapourSynthGenerationError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedVapourSynthTemplate:
    path: Path
    text: str
    template_hash: str


@dataclass(frozen=True, slots=True)
class ResolvedVapourSynthFilter:
    path: Path
    text: str
    script_hash: str
    entrypoint: str
    api_version: int


@dataclass(frozen=True, slots=True)
class VspipeCheckResult:
    stdout_excerpt: str
    stderr_excerpt: str


class VspipeError(RuntimeError):
    pass


class VspipeNotFoundError(VspipeError):
    pass


class VspipeTimeoutError(VspipeError):
    pass


class VspipeProcessError(VspipeError):
    pass


def normalize_template_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def build_template_hash(text: str) -> str:
    normalized = normalize_template_text(text)
    payload = f"{VAPOURSYNTH_TEMPLATE_HASH_CONTRACT}\0".encode() + normalized.encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def build_script_hash(text: str) -> str:
    normalized = normalize_template_text(text)
    payload = f"{VAPOURSYNTH_SCRIPT_HASH_CONTRACT}\0".encode() + normalized.encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def resolve_vapoursynth_template(
    profile: EncodingProfile,
) -> ResolvedVapourSynthTemplate | None:
    template_path = (
        profile.vapoursynth.template
        if profile.vapoursynth.mode == "custom_template"
        else None
    )
    if template_path is None:
        return None

    text = normalize_template_text(template_path.read_bytes().decode("utf-8"))
    return ResolvedVapourSynthTemplate(
        path=template_path,
        text=text,
        template_hash=build_template_hash(text),
    )


def resolve_vapoursynth_filter(
    profile: EncodingProfile,
) -> ResolvedVapourSynthFilter | None:
    if profile.vapoursynth.mode != "custom_filter":
        return None
    script_path = profile.vapoursynth.script
    if script_path is None:
        raise VapourSynthScriptPathError("custom_filter profile is missing script")

    text = normalize_template_text(script_path.read_bytes().decode("utf-8"))
    return ResolvedVapourSynthFilter(
        path=script_path,
        text=text,
        script_hash=build_script_hash(text),
        entrypoint=profile.vapoursynth.entrypoint,
        api_version=profile.vapoursynth.api_version,
    )


def resolve_workspace_script_path(workspace: WorkspaceContext, path: Path) -> Path:
    candidate = path if path.is_absolute() else workspace.scripts_dir / path
    scripts_dir = workspace.scripts_dir.resolve()
    resolved = candidate.resolve(strict=candidate.exists())
    try:
        resolved.relative_to(scripts_dir)
    except ValueError as exc:
        raise VapourSynthScriptPathError(
            f"VapourSynth script path escapes the workspace scripts directory: {path}"
        ) from exc
    return resolved


def build_vapoursynth_identity_hash(
    *,
    generator_version: int,
    mode: VapourSynthMode,
    output_format: VapourSynthOutputFormat,
    resize_filter: VapourSynthResizeFilter,
    template_hash: str | None,
    script_hash: str | None = None,
    filter_entrypoint: str | None = None,
    filter_api_version: int | None = None,
) -> str:
    if mode == "generated" and (
        template_hash is not None or script_hash is not None or filter_entrypoint is not None
    ):
        raise ValueError("generated mode must not include custom script data")
    if mode == "custom_template" and template_hash is None:
        raise ValueError("custom_template mode requires a template hash")
    if mode == "custom_template" and script_hash is not None:
        raise ValueError("custom_template mode must not include a script hash")
    if mode == "custom_filter" and script_hash is None:
        raise ValueError("custom_filter mode requires a script hash")
    if mode == "custom_filter" and template_hash is not None:
        raise ValueError("custom_filter mode must not include a template hash")

    payload_json = canonical_json(
        {
            "filter_api_version": filter_api_version,
            "filter_entrypoint": filter_entrypoint,
            "generator_version": generator_version,
            "mode": mode,
            "output_format": output_format,
            "resize_filter": resize_filter,
            "script_hash": script_hash,
            "template_hash": template_hash,
        }
    )
    payload = f"{VAPOURSYNTH_IDENTITY_HASH_CONTRACT}\0".encode() + payload_json.encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def generate_builtin_script(plan: TranscodePlan) -> str:
    _validate_builtin_plan(plan)
    source_path = repr(str(plan.vapoursynth.source_path))
    bestsource_plugin_path = resolve_bestsource_plugin_path()
    load_bestsource_plugin = ""
    if bestsource_plugin_path is not None:
        load_bestsource_plugin = (
            "if not hasattr(core, 'bs'):\n"
            f"    core.std.LoadPlugin(path={repr(str(bestsource_plugin_path))})\n"
            "\n"
        )

    return (
        "# Generated by avarch. Do not edit.\n"
        f"# Generator version: {plan.vapoursynth.generator_version}\n"
        f"# Plan hash: {plan.plan_hash}\n"
        f"# Profile: {plan.profile_name}\n"
        "\n"
        "import vapoursynth as vs\n"
        "\n"
        "core = vs.core\n"
        "\n"
        f"{load_bestsource_plugin}"
        f"source_path = {source_path}\n"
        "\n"
        "clip = core.bs.VideoSource(source=source_path)\n"
        "\n"
        "clip = core.resize.Spline36(\n"
        "    clip,\n"
        f"    width={plan.vapoursynth.target_width},\n"
        f"    height={plan.vapoursynth.target_height},\n"
        "    format=vs.YUV420P10,\n"
        ")\n"
        "\n"
        "clip.set_output(index=0)\n"
    )


def resolve_bestsource_plugin_path() -> Path | None:
    try:
        vapoursynth_utils = importlib.import_module("vapoursynth._utils")
    except ImportError:
        return None
    get_plugin_dir = getattr(vapoursynth_utils, "get_plugin_dir", None)
    if not callable(get_plugin_dir):
        return None
    plugin_dir = Path(str(get_plugin_dir()))
    plugin_path = plugin_dir / "libbestsource.so"
    if plugin_path.is_file():
        return plugin_path
    return None


def build_custom_template_preamble(plan: TranscodePlan) -> str:
    _validate_common_plan(plan)
    return (
        "# Generated by avarch. Do not edit this preamble.\n"
        "\n"
        f"AVARCH_SOURCE_PATH: str = {repr(str(plan.vapoursynth.source_path))}\n"
        f"AVARCH_VIDEO_STREAM_INDEX: int = {plan.vapoursynth.source_stream_index}\n"
        "\n"
        f"AVARCH_TARGET_WIDTH: int = {plan.vapoursynth.target_width}\n"
        f"AVARCH_TARGET_HEIGHT: int = {plan.vapoursynth.target_height}\n"
        "\n"
        f"AVARCH_INDEX_CACHE_DIR: str = {repr(str(plan.vapoursynth.index_cache_dir))}\n"
        "\n"
        f"AVARCH_PLAN_HASH: str = {repr(plan.plan_hash)}\n"
        f"AVARCH_PROFILE_NAME: str = {repr(plan.profile_name)}\n"
    )


def generate_custom_script(
    plan: TranscodePlan,
    *,
    template: ResolvedVapourSynthTemplate,
) -> str:
    _validate_common_plan(plan)
    if plan.vapoursynth.mode != "custom_template":
        raise VapourSynthTemplateError("custom template generation requires custom_template mode")
    if plan.vapoursynth.template_path != template.path:
        raise VapourSynthTemplateError("resolved template path does not match the plan")
    if plan.vapoursynth.template_hash != template.template_hash:
        raise VapourSynthTemplateError("resolved template hash does not match the plan")

    body = normalize_template_text(template.text)
    if body == "":
        raise VapourSynthTemplateError("custom VapourSynth template is empty")

    return f"{build_custom_template_preamble(plan)}\n{body.rstrip('\n')}\n"


def generate_custom_filter_script(
    plan: TranscodePlan,
    *,
    user_filter: ResolvedVapourSynthFilter,
) -> str:
    _validate_builtin_plan(plan)
    if plan.vapoursynth.mode != "custom_filter":
        raise VapourSynthTemplateError("custom filter generation requires custom_filter mode")
    if plan.vapoursynth.filter_path is None:
        raise VapourSynthTemplateError("custom filter plan is missing filter path")
    if plan.vapoursynth.filter_hash != user_filter.script_hash:
        raise VapourSynthTemplateError("resolved filter hash does not match the plan")
    if plan.vapoursynth.filter_entrypoint != user_filter.entrypoint:
        raise VapourSynthTemplateError("resolved filter entrypoint does not match the plan")

    source_path = repr(str(plan.vapoursynth.source_path))
    filter_path = repr(str(plan.vapoursynth.filter_path))
    entrypoint = repr(user_filter.entrypoint)
    bestsource_plugin_path = resolve_bestsource_plugin_path()
    source_is_hdr = is_hdr_source(
        source_color_transfer=plan.vapoursynth.source_color_transfer,
        source_hdr_metadata_present=plan.vapoursynth.source_hdr_metadata_present,
    )
    load_bestsource_plugin = ""
    if bestsource_plugin_path is not None:
        load_bestsource_plugin = (
            "if not hasattr(core, 'bs'):\n"
            f"    core.std.LoadPlugin(path={repr(str(bestsource_plugin_path))})\n"
            "\n"
        )

    return (
        "# Generated by avarch. Do not edit.\n"
        f"# Generator version: {plan.vapoursynth.generator_version}\n"
        f"# Plan hash: {plan.plan_hash}\n"
        f"# Profile: {plan.profile_name}\n"
        "\n"
        "import importlib.util\n"
        "from pathlib import Path\n"
        "\n"
        "import vapoursynth as vs\n"
        "\n"
        "from avarch.vpy_api import FilterContext\n"
        "\n"
        "core = vs.core\n"
        "\n"
        f"{load_bestsource_plugin}"
        f"source_path = {source_path}\n"
        f"filter_path = Path({filter_path})\n"
        "\n"
        "clip = core.bs.VideoSource(source=source_path)\n"
        "\n"
        "context = FilterContext(\n"
        f"    api_version={plan.vapoursynth.filter_api_version or 1},\n"
        "    source_path=Path(source_path),\n"
        "    source_relative_path=Path(source_path),\n"
        f"    video_stream_index={plan.vapoursynth.source_stream_index},\n"
        f"    source_width={plan.video.source_width},\n"
        f"    source_height={plan.video.source_height},\n"
        f"    target_width={plan.vapoursynth.target_width},\n"
        f"    target_height={plan.vapoursynth.target_height},\n"
        "    source_fps_num=None,\n"
        "    source_fps_den=None,\n"
        f"    source_is_hdr={source_is_hdr!r},\n"
        f"    color_primaries={plan.vapoursynth.source_color_primaries!r},\n"
        f"    color_transfer={plan.vapoursynth.source_color_transfer!r},\n"
        f"    color_matrix={plan.vapoursynth.source_color_space!r},\n"
        f"    work_dir=Path({repr(str(plan.temp_dir))}),\n"
        f"    cache_dir=Path({repr(str(plan.vapoursynth.index_cache_dir))}),\n"
        ")\n"
        "\n"
        "spec = importlib.util.spec_from_file_location('avarch_user_filter', filter_path)\n"
        "if spec is None or spec.loader is None:\n"
        "    raise RuntimeError(f'Unable to load VapourSynth filter: {filter_path}')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        f"entrypoint = getattr(module, {entrypoint})\n"
        "filtered = entrypoint(clip, context)\n"
        "if not isinstance(filtered, vs.VideoNode):\n"
        "    raise TypeError('custom VapourSynth filter must return a VideoNode')\n"
        "\n"
        "filtered = core.resize.Spline36(\n"
        "    filtered,\n"
        f"    width={plan.vapoursynth.target_width},\n"
        f"    height={plan.vapoursynth.target_height},\n"
        "    format=vs.YUV420P10,\n"
        ")\n"
        "\n"
        "filtered.set_output(index=0)\n"
    )


def generate_vapoursynth_script(
    plan: TranscodePlan,
    *,
    template: ResolvedVapourSynthTemplate | None,
    user_filter: ResolvedVapourSynthFilter | None = None,
) -> str:
    if plan.vapoursynth.mode == "generated":
        if template is not None:
            raise VapourSynthTemplateError("generated mode does not accept a template")
        if user_filter is not None:
            raise VapourSynthTemplateError("generated mode does not accept a custom filter")
        return generate_builtin_script(plan)
    if plan.vapoursynth.mode == "custom_filter":
        if user_filter is None:
            raise VapourSynthTemplateError("custom_filter mode requires a resolved filter")
        if template is not None:
            raise VapourSynthTemplateError("custom_filter mode does not accept a template")
        return generate_custom_filter_script(plan, user_filter=user_filter)
    if plan.vapoursynth.mode == "custom_template":
        if template is None:
            raise VapourSynthTemplateError("custom_template mode requires a resolved template")
        if user_filter is not None:
            raise VapourSynthTemplateError("custom_template mode does not accept a custom filter")
        return generate_custom_script(plan, template=template)

    raise VapourSynthTemplateError(f"unsupported VapourSynth mode: {plan.vapoursynth.mode}")


def validate_script_syntax(script: str) -> None:
    try:
        ast.parse(script, filename="<generated-vapoursynth>")
    except SyntaxError as exc:
        line = exc.text.strip() if exc.text else "unknown"
        raise VapourSynthSyntaxError(
            "VapourSynth script syntax error: "
            f"{exc.msg} at line {exc.lineno}, column {exc.offset}: {line}"
        ) from exc


def build_vspipe_info_command(
    script_path: Path,
    *,
    executable: str = "vspipe",
) -> list[str]:
    return [executable, "--info", str(script_path), "-"]


def check_vapoursynth_script(
    script_path: Path,
    *,
    executable: str = "vspipe",
    timeout_seconds: float = DEFAULT_VSPIPE_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> VspipeCheckResult:
    command = build_vspipe_info_command(script_path, executable=executable)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=env,
        )
    except FileNotFoundError as exc:
        raise VspipeNotFoundError(f"vspipe executable not found: {executable}") from exc
    except subprocess.TimeoutExpired as exc:
        stdout = _subprocess_output_text(exc.stdout)
        stderr = _subprocess_output_text(exc.stderr)
        raise VspipeTimeoutError(
            f"vspipe timed out after {timeout_seconds:g} seconds\n"
            f"stdout:\n{build_bounded_output_excerpt(stdout)}\n"
            f"stderr:\n{build_bounded_output_excerpt(stderr)}"
        ) from exc

    stdout_excerpt = build_bounded_output_excerpt(result.stdout)
    stderr_excerpt = build_bounded_output_excerpt(result.stderr)
    if result.returncode != 0:
        raise VspipeProcessError(
            f"vspipe failed with exit code {result.returncode}\n"
            f"stdout:\n{stdout_excerpt}\n"
            f"stderr:\n{stderr_excerpt}"
        )

    return VspipeCheckResult(
        stdout_excerpt=stdout_excerpt,
        stderr_excerpt=stderr_excerpt,
    )


def build_bounded_output_excerpt(
    value: str,
    *,
    max_bytes: int = MAX_VSPIPE_EXCERPT_BYTES,
) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    if max_bytes <= 0:
        return ""

    head_len = max(0, max_bytes // 2)
    tail_len = max(0, max_bytes - head_len)
    while True:
        head = _decode_prefix(raw, head_len)
        tail = _decode_suffix(raw, tail_len)
        omitted = len(raw) - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
        marker = f"\n... [output truncated: {omitted} bytes omitted] ...\n"
        excerpt = f"{head}{marker}{tail}"
        excerpt_len = len(excerpt.encode("utf-8"))
        if excerpt_len <= max_bytes:
            return excerpt
        overflow = excerpt_len - max_bytes
        if tail_len >= head_len and tail_len > 0:
            tail_len = max(0, tail_len - overflow)
        elif head_len > 0:
            head_len = max(0, head_len - overflow)
        else:
            return marker.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def is_hdr_source(
    *,
    source_color_transfer: str | None,
    source_hdr_metadata_present: bool,
) -> bool:
    transfer = source_color_transfer.strip().lower() if source_color_transfer else None
    return transfer in HDR_TRANSFER_VALUES or source_hdr_metadata_present


def _validate_builtin_plan(plan: TranscodePlan) -> None:
    _validate_common_plan(plan)
    if plan.vapoursynth.mode not in {"generated", "custom_filter"}:
        raise VapourSynthGenerationError("built-in graph generation requires generated mode")
    if plan.vapoursynth.template_hash is not None or plan.vapoursynth.template_path is not None:
        raise VapourSynthGenerationError("generated mode must not include template data")
    if plan.video.source_codec.strip().lower() not in _SUPPORTED_SOURCE_CODECS:
        raise UnsupportedSourceFormatError(f"unsupported source codec: {plan.video.source_codec}")
    if plan.vapoursynth.source_pix_fmt is None:
        raise UnsupportedSourceFormatError("source pixel format is missing")
    if plan.vapoursynth.source_pix_fmt not in _SUPPORTED_PIXEL_FORMATS:
        raise UnsupportedSourceFormatError(
            f"unsupported source pixel format: {plan.vapoursynth.source_pix_fmt}"
        )
    if is_hdr_source(
        source_color_transfer=plan.vapoursynth.source_color_transfer,
        source_hdr_metadata_present=plan.vapoursynth.source_hdr_metadata_present,
    ):
        raise HdrProcessingNotImplementedError(
            "built-in VapourSynth generation does not support HDR input; use a custom template"
        )


def _validate_common_plan(plan: TranscodePlan) -> None:
    if plan.plan_hash == "":
        raise VapourSynthGenerationError("plan hash must be finalized before script generation")
    if plan.vapoursynth.source_path != plan.input_path:
        raise VapourSynthGenerationError("VapourSynth source path must match plan input path")
    if plan.vapoursynth.script_path != plan.artifacts.vapoursynth_script:
        raise VapourSynthGenerationError("VapourSynth script path must match artifact path")
    if plan.vapoursynth.source_stream_index != plan.video.source_stream_index:
        raise VapourSynthGenerationError("VapourSynth stream index must match selected video")
    if plan.vapoursynth.target_width != plan.video.target_width:
        raise VapourSynthGenerationError("VapourSynth target width must match selected video")
    if plan.vapoursynth.target_height != plan.video.target_height:
        raise VapourSynthGenerationError("VapourSynth target height must match selected video")
    if plan.vapoursynth.output_format != "YUV420P10":
        raise VapourSynthGenerationError("unsupported VapourSynth output format")
    if plan.vapoursynth.resize_filter != "spline36":
        raise VapourSynthGenerationError("unsupported VapourSynth resize filter")
    if plan.vapoursynth.target_width < 2 or plan.vapoursynth.target_width % 2 != 0:
        raise VapourSynthGenerationError("target width must be positive and even")
    if plan.vapoursynth.target_height < 2 or plan.vapoursynth.target_height % 2 != 0:
        raise VapourSynthGenerationError("target height must be positive and even")


def _decode_prefix(value: bytes, max_length: int) -> str:
    return value[:max_length].decode("utf-8", errors="ignore")


def _decode_suffix(value: bytes, max_length: int) -> str:
    if max_length <= 0:
        return ""
    return value[-max_length:].decode("utf-8", errors="ignore")


def _subprocess_output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
