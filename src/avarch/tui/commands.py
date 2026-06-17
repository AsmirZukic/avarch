from __future__ import annotations

from dataclasses import dataclass

from avarch.tui.state import PRIMARY_ROUTES, TuiRoute


@dataclass(frozen=True, slots=True)
class CommandSpec:
    command_id: str
    title: str
    route: TuiRoute | None = None


def global_command_specs() -> tuple[CommandSpec, ...]:
    route_commands = tuple(
        CommandSpec(command_id=f"go:{route.value}", title=f"Go to {route.label}", route=route)
        for route in PRIMARY_ROUTES
    )
    return (
        *route_commands,
        CommandSpec(command_id="refresh", title="Refresh"),
        CommandSpec(command_id="quit", title="Quit"),
    )
