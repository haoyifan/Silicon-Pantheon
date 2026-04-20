"""Built-in win-condition rule classes.

Each class self-registers via the @register decorator so it shows up
in the DSL lookup table under its canonical name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from silicon_pantheon.server.engine.state import Pos, Team
from silicon_pantheon.server.engine.win_conditions.base import WinResult, register


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

    def describe_progress(self, state, viewer) -> str | None:
        """Closest-own-unit-to-enemy-fort progress hint. For each
        enemy fort, the Manhattan distance of the viewer's nearest
        unit to that fort; plus a flag if the viewer already has a
        unit sitting on one (about to win at end_turn)."""
        enemy = viewer.other()
        enemy_forts = []
        for pos, tile in state.board.tiles.items():
            if tile.is_fort and tile.fort_owner is enemy:
                enemy_forts.append(pos)
        if not enemy_forts:
            return None
        my_units = [u for u in state.units_of(viewer) if u.alive]
        if not my_units:
            return f"Seize any enemy fort to win: {len(enemy_forts)} enemy fort(s); you have no units left"
        parts: list[str] = []
        for fp in enemy_forts:
            closest = min(my_units, key=lambda u: u.pos.manhattan(fp))
            d = closest.pos.manhattan(fp)
            if d == 0:
                parts.append(
                    f"({fp.x},{fp.y}): {closest.id} IS ON IT — "
                    "end_turn here WINS"
                )
            else:
                parts.append(
                    f"({fp.x},{fp.y}): closest is {closest.id} at "
                    f"({closest.pos.x},{closest.pos.y}), {d} tile(s) away"
                )
        return "Seize any enemy fort to win: " + "; ".join(parts)


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

    def describe_progress(self, state, viewer) -> str | None:
        enemy = viewer.other()
        enemy_alive = sum(1 for u in state.units_of(enemy) if u.alive)
        my_alive = sum(1 for u in state.units_of(viewer) if u.alive)
        return (
            f"Eliminate all enemies to win: {enemy_alive} enemy "
            f"unit(s) still alive; you have {my_alive}"
        )


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

    def describe_progress(self, state, viewer) -> str | None:
        cap = self.turns if self.turns is not None else state.max_turns
        remaining = max(0, cap - state.turn + 1)
        return (
            f"Turn cap: {remaining} turn(s) remain before draw "
            f"(turn {state.turn} of {cap})"
        )


@register("protect_unit")
@dataclass
class ProtectUnit:
    """Fires when a specific unit dies; its team loses."""

    unit_id: str = ""
    owning_team: str = "blue"
    trigger: str = "end_turn"  # checked at end_turn too in case of HP=0 sneak

    def check(self, state, hook, **_) -> WinResult | None:
        # The VIP is "lost" if it has died this match — either visibly
        # in the dict at hp=0 (rare; combat removes immediately) or
        # already deleted (the common case after _apply_attack). The
        # dead_unit_ids set is the single source of truth.
        u = state.units.get(self.unit_id)
        if u is not None and u.alive:
            return None
        if u is None and self.unit_id not in getattr(state, "dead_unit_ids", set()):
            # Unit never existed — treat as not-yet-lost (the scenario
            # may simply have a stale unit_id; don't end the match on
            # that). If it had existed and died, dead_unit_ids would
            # know.
            return None
        loser = self.owning_team
        winner = "red" if loser == "blue" else "blue"
        return WinResult(
            winner=winner,
            reason="vip_lost",
            details={"dead_unit": self.unit_id},
        )

    def describe_progress(self, state, viewer) -> str | None:
        u = state.units.get(self.unit_id)
        viewer_is_protector = viewer.value == self.owning_team
        if u is not None and u.alive:
            hp_frac = f"HP {u.hp}/{u.stats.hp_max}"
            pos = f"at ({u.pos.x},{u.pos.y})"
            if viewer_is_protector:
                return (
                    f"PROTECT your VIP {self.unit_id} ({hp_frac}, "
                    f"{pos}): if it dies you lose immediately"
                )
            return (
                f"KILL enemy VIP {self.unit_id} ({hp_frac}, {pos}): "
                f"killing it wins the match"
            )
        # VIP not in live units. Distinguish "died this match" from
        # "never existed (typo'd unit_id in the scenario YAML)" — the
        # latter is silent because there's no game-state signal to
        # report.
        if self.unit_id not in getattr(state, "dead_unit_ids", set()):
            return None
        if viewer_is_protector:
            return f"Your VIP {self.unit_id} is DEAD — you will lose at end_turn"
        return f"Enemy VIP {self.unit_id} is DEAD — you win at end_turn"


@register("protect_unit_survives")
@dataclass
class ProtectUnitSurvives:
    """The protector wins if the VIP is still alive when the turn
    cap is reached.

    Complement to `protect_unit` (which handles the VIP-dies loss
    case). Scenarios where "hold out until time runs out" is victory
    should declare BOTH rules: `protect_unit` catches the early loss,
    `protect_unit_survives` catches the late win. The turn-cap draw
    (`max_turns_draw`) should remain last as a fallthrough so
    scenarios that don't declare this rule still draw normally —
    which is the point: this rule is purely opt-in.

    Fires only at end_turn when state.turn > cap. If the VIP died
    earlier, `protect_unit` has already fired and the match is over;
    this rule is a no-op in that case.
    """

    unit_id: str = ""
    owning_team: str = "blue"
    turns: int | None = None  # override; if None, use state.max_turns
    trigger: str = "end_turn"

    def check(self, state, hook, **_) -> WinResult | None:
        if hook != "end_turn":
            return None
        cap = self.turns if self.turns is not None else state.max_turns
        if state.turn <= cap:
            return None
        u = state.units.get(self.unit_id)
        if u is not None and u.alive:
            return WinResult(
                winner=self.owning_team,
                reason="protect_survived",
                details={"vip": self.unit_id, "turns": cap},
            )
        return None

    def describe_progress(self, state, viewer) -> str | None:
        cap = self.turns if self.turns is not None else state.max_turns
        remaining = max(0, cap - state.turn + 1)
        u = state.units.get(self.unit_id)
        vip_alive = u is not None and u.alive
        if not vip_alive:
            # protect_unit's describe covers the dead-VIP case; stay
            # silent here so we don't duplicate.
            return None
        is_protector = viewer.value == self.owning_team
        if is_protector:
            return (
                f"HOLD OUT TO WIN: if {self.unit_id} survives to the "
                f"turn cap, you win. {remaining} turn(s) remain "
                "(including this one)."
            )
        return (
            f"BREAK THROUGH BEFORE TIME RUNS OUT: you must kill "
            f"{self.unit_id} (or eliminate all enemies) within "
            f"{remaining} turn(s); otherwise {self.owning_team} "
            "wins at the turn cap."
        )


@register("reach_tile")
@dataclass
class ReachTile:
    """A specific unit (or any unit of a team) ending its turn on a
    tile → win.

    Two shapes are accepted:
      - Single tile: ``pos: {x, y}``
      - Zone: ``positions: [{x, y}, {x, y}, ...]`` — any of the
        listed tiles triggers the win. Use this when the objective
        is a multi-tile area (e.g. a 3x4 bunker) instead of one
        point. Renders as a compact bounding-box summary in
        ``describe_progress`` so the scoreboard stays readable.

    Exactly one of ``pos`` or ``positions`` must be set. If both
    are set, ``positions`` wins and ``pos`` is ignored.
    """

    team: str = "blue"
    pos: dict | None = None
    positions: list[dict] | None = None
    unit_id: str | None = None
    trigger: str = "end_turn"

    def _targets(self) -> list[Pos]:
        if self.positions:
            return [
                Pos(int(p["x"]), int(p["y"])) for p in self.positions
            ]
        if self.pos is not None:
            return [Pos(int(self.pos["x"]), int(self.pos["y"]))]
        return []

    def check(self, state, hook, **_) -> WinResult | None:
        if hook != "end_turn":
            return None
        targets = self._targets()
        if not targets:
            return None
        target_set = set(targets)
        for u in state.units_of(Team(self.team)):
            if self.unit_id is not None and u.id != self.unit_id:
                continue
            if u.pos in target_set:
                return WinResult(
                    winner=self.team,
                    reason="reach_tile",
                    details={
                        "unit": u.id,
                        "pos": u.pos.to_dict(),
                    },
                )
        return None

    def _describe_targets(self) -> str:
        """Render targets compactly for describe_progress.

        Single tile: "(x,y)". Small zone (≤4 tiles): list them.
        Rectangular zone: bounding-box "(x0-x1,y0-y1)". Otherwise:
        bbox with a "(N tiles)" suffix.
        """
        targets = self._targets()
        if not targets:
            return "(no target)"
        if len(targets) == 1:
            t = targets[0]
            return f"({t.x},{t.y})"
        xs = [t.x for t in targets]
        ys = [t.y for t in targets]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        # If targets exactly fill the bounding box, render as a clean range.
        span = (x1 - x0 + 1) * (y1 - y0 + 1)
        if span == len(targets):
            xrange = f"{x0}" if x0 == x1 else f"{x0}-{x1}"
            yrange = f"{y0}" if y0 == y1 else f"{y0}-{y1}"
            return f"any tile in (x={xrange}, y={yrange})"
        if len(targets) <= 4:
            return "any of " + ", ".join(f"({t.x},{t.y})" for t in targets)
        return (
            f"any of {len(targets)} tiles in "
            f"(x={x0}-{x1}, y={y0}-{y1})"
        )

    def describe_progress(self, state, viewer) -> str | None:
        targets = self._targets()
        if not targets:
            return None
        target_set = set(targets)
        label = self._describe_targets()
        is_mine = viewer.value == self.team
        eligible = [u for u in state.units_of(Team(self.team)) if u.alive]
        if self.unit_id is not None:
            eligible = [u for u in eligible if u.id == self.unit_id]
        if not eligible:
            if is_mine:
                return (
                    f"Reach {label} with your "
                    f"{'team' if self.unit_id is None else self.unit_id} "
                    "to win: no eligible unit alive"
                )
            return (
                f"Opponent wins if their "
                f"{'team' if self.unit_id is None else self.unit_id} "
                f"reaches {label}: no eligible unit alive"
            )
        # Closest unit over any target tile.
        closest = min(
            eligible,
            key=lambda u: min(u.pos.manhattan(t) for t in targets),
        )
        d = min(closest.pos.manhattan(t) for t in targets)
        who = "your" if is_mine else f"{self.team}'s"
        win_verb = "to win" if is_mine else "and that team wins"
        already_there = closest.pos in target_set
        if already_there:
            return (
                f"Reach {label} with {who} "
                f"{'units' if self.unit_id is None else self.unit_id} "
                f"{win_verb}: {closest.id} is ALREADY on the objective"
            )
        return (
            f"Reach {label} with {who} "
            f"{'units' if self.unit_id is None else self.unit_id} "
            f"{win_verb}: closest is {closest.id} at "
            f"({closest.pos.x},{closest.pos.y}), {d} tile(s) away"
        )


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

    def describe_progress(self, state, viewer) -> str | None:
        if self.pos is None:
            return None
        target = Pos(int(self.pos["x"]), int(self.pos["y"]))
        is_mine = viewer.value == self.team
        occupant = state.unit_at(target)
        held_by_right_team = (
            occupant is not None and occupant.owner is Team(self.team)
        )
        who = "your" if is_mine else f"{self.team}'s"
        win_verb = "to win" if is_mine else "(opponent wins)"
        status = (
            f"held {self._count}/{self.consecutive_turns} consecutive end_turn(s)"
            if held_by_right_team
            else f"currently NOT held (counter reset, needs {self.consecutive_turns} in a row)"
        )
        return (
            f"Hold ({target.x},{target.y}) with {who} units for "
            f"{self.consecutive_turns} consecutive end_turns {win_verb}: "
            f"{status}"
        )


@register("reach_goal_line")
@dataclass
class ReachGoalLine:
    """Any unit of `team` crosses a row/column → win.

    `direction` controls how "crossed" is interpreted:
      - "crosses" (default): the team has reached or gone PAST the
        line. The side counts as crossed is inferred from where the
        team's own live units mostly sit right now — if the median
        team-coord is above `value`, the team needs coord <= value;
        if below, the team needs coord >= value. This matches the
        natural scenario semantic ("the Persians win if any of them
        slip through to x ≤ 1").
      - "exact": coord must equal value exactly (legacy behavior).
      - "<=" / ">=": explicit direction, bypasses inference.

    Scenarios written before the `direction` field existed still
    work — all of them were relying on the intended "crossed"
    semantic, so "crosses" is the correct default.
    """

    team: str = "blue"
    axis: str = "x"  # "x" or "y"
    value: int = 0
    trigger: str = "end_turn"
    direction: str = "crosses"  # "crosses" | "exact" | "<=" | ">="

    def _effective_direction(self, state) -> str:
        if self.direction != "crosses":
            return self.direction
        coords = [
            (u.pos.x if self.axis == "x" else u.pos.y)
            for u in state.units_of(Team(self.team))
            if u.alive
        ]
        if not coords:
            return "exact"
        median = sorted(coords)[len(coords) // 2]
        return "<=" if median > self.value else ">="

    def _has_crossed(self, coord: int, direction: str) -> bool:
        if direction == "<=":
            return coord <= self.value
        if direction == ">=":
            return coord >= self.value
        return coord == self.value

    def check(self, state, hook, **_) -> WinResult | None:
        if hook != "end_turn":
            return None
        direction = self._effective_direction(state)
        for u in state.units_of(Team(self.team)):
            coord = u.pos.x if self.axis == "x" else u.pos.y
            if self._has_crossed(coord, direction):
                return WinResult(
                    winner=self.team,
                    reason="reach_goal_line",
                    details={
                        "unit": u.id,
                        "pos": u.pos.to_dict(),
                    },
                )
        return None

    def describe_progress(self, state, viewer) -> str | None:
        is_mine = viewer.value == self.team
        eligible = [u for u in state.units_of(Team(self.team)) if u.alive]
        if not eligible:
            return None
        direction = self._effective_direction(state)

        def _dist(u):
            coord = u.pos.x if self.axis == "x" else u.pos.y
            # A unit that has already crossed reports 0 — don't show
            # "2 tiles away" for someone who's actually past the line.
            if self._has_crossed(coord, direction):
                return 0
            return abs(coord - self.value)

        closest = min(eligible, key=_dist)
        d = _dist(closest)
        who = "your" if is_mine else f"{self.team}'s"
        win_verb = "to win" if is_mine else "and that team wins"
        return (
            f"Cross {self.axis}={self.value} with any {who} unit "
            f"{win_verb}: closest is {closest.id} at "
            f"({closest.pos.x},{closest.pos.y}), {d} tile(s) away"
        )


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

    def describe_progress(self, state, viewer) -> str | None:
        """If the plugin module exposes a companion describe function
        (name = check_fn + "_describe"), call it and use its string.
        Otherwise return a generic fallback so the agent at least
        knows a custom rule is in play."""
        ns = getattr(state, "_plugin_namespace", {}) or {}
        describe_name = self.check_fn + "_describe"
        fn = ns.get(describe_name)
        if callable(fn):
            try:
                out = fn(state, viewer, **(self.kwargs or {}))
                if isinstance(out, str) and out.strip():
                    return out.strip()
            except Exception:
                import logging as _logging
                _logging.getLogger("silicon.engine").exception(
                    "plugin describe_progress %r raised; omitting hint",
                    describe_name,
                )
                return None
        return (
            f"(custom plugin win rule `{self.check_fn}` in play — "
            "see scenario rules for details)"
        )
