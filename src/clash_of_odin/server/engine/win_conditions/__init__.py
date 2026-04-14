"""Declarative win conditions — evaluated at engine hooks, first match wins."""

from clash_of_odin.server.engine.win_conditions.base import (
    WinCondition,
    WinResult,
    build_conditions,
    default_conditions,
)

__all__ = ["WinCondition", "WinResult", "build_conditions", "default_conditions"]
