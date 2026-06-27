from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


class VpyPluginInventoryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VpyPluginInfo:
    namespace: str
    name: str
    version: str | None
    source: str
    path: Path | None


def list_vapoursynth_plugins() -> tuple[VpyPluginInfo, ...]:
    try:
        import vapoursynth as vs  # type: ignore[import-not-found]
    except ImportError as exc:
        raise VpyPluginInventoryError("VapourSynth is not available in this runtime.") from exc

    core = vs.core
    plugins = getattr(core, "plugins", None)
    if not callable(plugins):
        raise VpyPluginInventoryError("VapourSynth runtime does not expose plugin inventory.")

    plugin_values = cast(Iterable[Any], plugins())
    return tuple(
        sorted(
            (plugin_info_from_object(plugin) for plugin in plugin_values),
            key=_plugin_sort_key,
        )
    )


def plugin_info_from_object(plugin: Any) -> VpyPluginInfo:
    namespace = _text_attr(plugin, "namespace", default="-")
    name = _text_attr(plugin, "name", "identifier", default=namespace)
    version = _optional_text_attr(plugin, "version")
    path_text = _optional_text_attr(plugin, "path", "filename")
    path = Path(path_text) if path_text else None
    source = _plugin_source(path)
    return VpyPluginInfo(
        namespace=namespace,
        name=name,
        version=version,
        source=source,
        path=path,
    )


def _plugin_source(path: Path | None) -> str:
    if path is None:
        return "builtin"
    path_text = str(path)
    if "/.avarch/vpy/environments/" in path_text:
        return "workspace"
    return "runtime"


def _text_attr(plugin: Any, *names: str, default: str) -> str:
    value = _optional_text_attr(plugin, *names)
    return value if value is not None else default


def _optional_text_attr(plugin: Any, *names: str) -> str | None:
    for name in names:
        value = getattr(plugin, name, None)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _plugin_sort_key(plugin: VpyPluginInfo) -> tuple[str, str]:
    return (plugin.namespace, plugin.name)
