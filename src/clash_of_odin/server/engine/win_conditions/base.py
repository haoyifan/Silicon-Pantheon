"""Framework for declarative win conditions.

The engine exposes two hook points that condition types consult:

  - `end_turn` — fires inside `_apply_end_turn` after handover, before
    the fall-through "no winner" return.
  - `on_unit_killed` — fires inside attack resolution when HP hits 0.

`check(state, hook, **event_kwargs)` returns a `WinResult` to declare
a winner / draw / loss, or None to pass.

Scenarios list conditions in YAML; `build_conditions(cfg_list)`
instantiates the right classes with their kwargs. `default_conditions()`
returns the equivalent of today's hardcoded checks for scenarios that
don't define their own list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class WinResult:
    winner: str | None  # "blue" | "red" | None (None = draw)
    reason: str
    details: dict[str, Any] | None = None


class WinCondition(Protocol):
    """Protocol every rule implements."""

    trigger: str  # "end_turn" | "on_unit_killed" (accept multiple via 'any')

    def check(
        self,
        state,  # GameState
        hook: str,
        **event_kwargs,
    ) -> WinResult | None: ...


# Registry populated by submodules that import this module.
_REGISTRY: dict[str, type] = {}


def register(name: str):
    """Decorator used by each rule class to wire itself into the DSL."""

    def _wrap(cls):
        _REGISTRY[name] = cls
        return cls

    return _wrap


def build_conditions(specs: list[dict]) -> list[WinCondition]:
    """Instantiate rule objects from their YAML entries.

    Each entry must have a `type` key; remaining keys become kwargs to
    the class constructor.
    """
    # Import the concrete rules so they self-register in _REGISTRY.
    from clash_of_odin.server.engine.win_conditions import rules  # noqa: F401

    out: list[WinCondition] = []
    for spec in specs or []:
        type_name = spec.get("type")
        if type_name not in _REGISTRY:
            raise ValueError(
                f"unknown win_condition type {type_name!r}; known: "
                f"{sorted(_REGISTRY)}"
            )
        cls = _REGISTRY[type_name]
        kwargs = {k: v for k, v in spec.items() if k != "type"}
        out.append(cls(**kwargs))
    return out


def default_conditions() -> list[WinCondition]:
    """Rule list equivalent to the engine's pre-DSL hardcoded checks.

    Used by scenarios that don't declare `win_conditions:` explicitly,
    so old YAML files keep playing the same way.
    """
    return build_conditions(
        [
            {"type": "seize_enemy_fort"},
            {"type": "eliminate_all_enemy_units"},
            {"type": "max_turns_draw"},
        ]
    )
