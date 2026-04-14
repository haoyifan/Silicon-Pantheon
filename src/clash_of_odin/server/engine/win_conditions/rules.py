"""Built-in win-condition rule classes.

Each class self-registers via the @register decorator so it shows up
in the DSL lookup table under its canonical name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clash_of_odin.server.engine.state import Pos, Team
from clash_of_odin.server.engine.win_conditions.base import WinResult, register


@register("seize_enemy_fort")
@dataclass
class SeizeEnemyFort:
    trigger: str = "end_turn"

    def check(self, state, hook, **_) -> WinResult | None:
        if hook != "end_turn":
            return None
        active = state.active_player
        enemy = active.other()
        for u in state.units_of(active):
            tile = state.board.tile(u.pos)
            if tile.is_fort and tile.fort_owner is enemy:
                return WinResult(
                    winner=active.value,
                    reason="seize",
                    details={
                        "seized_at": u.pos.to_dict(),
                        "seized_by_unit": u.id,
                    },
                )
        return None


@register("eliminate_all_enemy_units")
@dataclass
class EliminateAllEnemyUnits:
    trigger: str = "end_turn"

    def check(self, state, hook, **_) -> WinResult | None:
        if hook != "end_turn":
            return None
        # Check AFTER the active_player has flipped in _apply_end_turn —
        # so the 'incoming' team with no units has lost.
        if not state.units_of(state.active_player):
            winner = state.active_player.other()
            return WinResult(winner=winner.value, reason="elimination")
        return None


@register("max_turns_draw")
@dataclass
class MaxTurnsDraw:
    turns: int | None = None  # override; if None, use state.max_turns
    trigger: str = "end_turn"

    def check(self, state, hook, **_) -> WinResult | None:
        if hook != "end_turn":
            return None
        cap = self.turns if self.turns is not None else state.max_turns
        if state.turn > cap:
            return WinResult(winner=None, reason="max_turns")
        return None


@register("protect_unit")
@dataclass
class ProtectUnit:
    """Fires when a specific unit dies; its team loses."""

    unit_id: str = ""
    owning_team: str = "blue"
    trigger: str = "end_turn"  # checked at end_turn too in case of HP=0 sneak

    def check(self, state, hook, **_) -> WinResult | None:
        u = state.units.get(self.unit_id)
        if u is None:
            return None
        if u.alive:
            return None
        loser = self.owning_team
        winner = "red" if loser == "blue" else "blue"
        return WinResult(
            winner=winner,
            reason="vip_lost",
            details={"dead_unit": self.unit_id},
        )


@register("reach_tile")
@dataclass
class ReachTile:
    """A specific unit (or any unit of a team) ending its turn on a tile → win."""

    team: str = "blue"
    pos: dict | None = None
    unit_id: str | None = None
    trigger: str = "end_turn"

    def check(self, state, hook, **_) -> WinResult | None:
        if hook != "end_turn":
            return None
        if self.pos is None:
            return None
        target = Pos(int(self.pos["x"]), int(self.pos["y"]))
        for u in state.units_of(Team(self.team)):
            if self.unit_id is not None and u.id != self.unit_id:
                continue
            if u.pos == target:
                return WinResult(
                    winner=self.team,
                    reason="reach_tile",
                    details={
                        "unit": u.id,
                        "pos": u.pos.to_dict(),
                    },
                )
        return None


@register("hold_tile")
@dataclass
class HoldTile:
    """Hold a tile with any unit of `team` for N consecutive end_turns → win."""

    team: str = "blue"
    pos: dict | None = None
    consecutive_turns: int = 3
    # Runtime-tracked counter. Note: a scenario can't provide this
    # directly via YAML (it's not a field they'd pass), but we need a
    # default to keep the dataclass usable.
    _count: int = 0
    trigger: str = "end_turn"

    def check(self, state, hook, **_) -> WinResult | None:
        if hook != "end_turn":
            return None
        if self.pos is None:
            return None
        target = Pos(int(self.pos["x"]), int(self.pos["y"]))
        occupant = state.unit_at(target)
        on_tile = occupant is not None and occupant.owner is Team(self.team)
        if on_tile:
            self._count += 1
        else:
            self._count = 0
        if self._count >= self.consecutive_turns:
            return WinResult(
                winner=self.team,
                reason="hold_tile",
                details={"pos": target.to_dict(), "turns": self._count},
            )
        return None


@register("reach_goal_line")
@dataclass
class ReachGoalLine:
    """Any unit of `team` crosses a row/column → win."""

    team: str = "blue"
    axis: str = "x"  # "x" or "y"
    value: int = 0
    trigger: str = "end_turn"

    def check(self, state, hook, **_) -> WinResult | None:
        if hook != "end_turn":
            return None
        for u in state.units_of(Team(self.team)):
            coord = u.pos.x if self.axis == "x" else u.pos.y
            if coord == self.value:
                return WinResult(
                    winner=self.team,
                    reason="reach_goal_line",
                    details={
                        "unit": u.id,
                        "pos": u.pos.to_dict(),
                    },
                )
        return None


@register("plugin")
@dataclass
class PluginRule:
    """Delegate to a scenario-provided callable.

    The callable is resolved lazily from the plugin module's namespace
    stored on the GameState as `state._plugin_namespace` (populated by
    the scenario loader once it loads rules.py).
    """

    module: str = ""
    check_fn: str = ""  # function name in the plugin module
    kwargs: dict[str, Any] | None = None
    trigger: str = "end_turn"

    def check(self, state, hook, **_) -> WinResult | None:
        ns = getattr(state, "_plugin_namespace", {}) or {}
        fn = ns.get(self.check_fn)
        if not callable(fn):
            return None
        result = fn(state, hook, **(self.kwargs or {}))
        if result is None:
            return None
        if isinstance(result, WinResult):
            return result
        # Allow plugins to return a plain dict.
        return WinResult(
            winner=result.get("winner"),
            reason=str(result.get("reason", "plugin")),
            details=result.get("details"),
        )
