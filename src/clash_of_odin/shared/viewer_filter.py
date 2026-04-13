"""Viewer filter: central info-redaction for fog of war.

Every tool response that would reveal state passes through this module.
It is the one audit surface for "does this tool leak information" —
if you see a fog bug, it's a bug in this file and a few callers.

Design invariants (enforced by tests in tests/test_fog.py):

  - An enemy unit never appears in a view when its position isn't in
    `visible_tiles(state, ctx.team)` under modes `classic` /
    `line_of_sight`.
  - Own-team units always appear.
  - `none` mode returns state unchanged (filter is identity).
  - For `classic`, terrain reveal is a superset of `line_of_sight`
    given the same `ever_seen`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from clash_of_odin.server.engine.serialize import state_to_dict
from clash_of_odin.server.engine.state import GameState, Pos, Team, TerrainType
from clash_of_odin.shared.fog import visible_tiles

FogMode = Literal["none", "classic", "line_of_sight"]


@dataclass
class ViewerContext:
    team: Team
    fog_mode: FogMode = "none"
    # Set of tiles ever observed by this team (classic mode only).
    # Callers are responsible for updating this at end of each half-turn.
    ever_seen: frozenset[Pos] = frozenset()


def currently_visible(state: GameState, ctx: ViewerContext) -> set[Pos]:
    if ctx.fog_mode == "none":
        # Entire board.
        return {
            Pos(x, y)
            for x in range(state.board.width)
            for y in range(state.board.height)
        }
    return visible_tiles(state, ctx.team)


def _filtered_unit_dict(unit_dict: dict[str, Any]) -> dict[str, Any]:
    """Identity for now — reserved for future partial-info granularity."""
    return unit_dict


def filter_state(state: GameState, ctx: ViewerContext) -> dict[str, Any]:
    """Return state_to_dict filtered for `ctx.team` under `ctx.fog_mode`."""
    raw = state_to_dict(state, viewer=ctx.team)
    if ctx.fog_mode == "none":
        return raw
    visible = currently_visible(state, ctx)

    # Units: keep own-team always; enemies only if their position is
    # currently visible.
    raw_units = raw.get("units", [])
    filtered_units: list[dict[str, Any]] = []
    for u in raw_units:
        owner = u.get("owner")
        pos = u.get("pos") or {}
        ux, uy = int(pos.get("x", -1)), int(pos.get("y", -1))
        alive = u.get("alive", u.get("hp", 0) > 0)
        if owner == ctx.team.value:
            filtered_units.append(_filtered_unit_dict(u))
            continue
        # Dead enemy units are known history — once you killed them
        # they're on the record; showing the corpse doesn't leak any
        # live intel. This avoids making dead enemies vanish from a
        # team's units table when they happen to die on a tile that
        # isn't in current sight.
        if not alive:
            filtered_units.append(_filtered_unit_dict(u))
            continue
        if Pos(ux, uy) in visible:
            filtered_units.append(_filtered_unit_dict(u))
    raw["units"] = filtered_units

    # Board terrain: in classic, tiles ever seen show their real terrain;
    # un-seen tiles are masked. In line_of_sight, only currently visible
    # tiles show terrain.
    board = raw.get("board", {})
    tiles = board.get("tiles", [])
    if ctx.fog_mode == "classic":
        known = set(ctx.ever_seen) | visible
    else:  # line_of_sight
        known = visible
    masked_tiles: list[dict[str, Any]] = []
    for t in tiles:
        tx = int(t.get("x", 0))
        ty = int(t.get("y", 0))
        if Pos(tx, ty) in known:
            masked_tiles.append(t)
        else:
            masked_tiles.append({"x": tx, "y": ty, "type": "unknown"})
    board["tiles"] = masked_tiles
    raw["board"] = board

    # Annotate so clients know what to render as "?".
    raw["_visible_tiles"] = sorted(
        (p.x, p.y) for p in visible
    )
    return raw


def filter_unit(
    unit_id: str, unit_dict: dict[str, Any], state: GameState, ctx: ViewerContext
) -> dict[str, Any] | None:
    """Filtered per-unit view. Returns None if the unit is not visible."""
    if ctx.fog_mode == "none":
        return unit_dict
    owner = unit_dict.get("owner")
    if owner == ctx.team.value:
        return unit_dict
    pos = unit_dict.get("pos") or {}
    p = Pos(int(pos.get("x", -1)), int(pos.get("y", -1)))
    if p in currently_visible(state, ctx):
        return unit_dict
    return None


def filter_threat_map(
    threats: dict[str, Any], state: GameState, ctx: ViewerContext
) -> dict[str, Any]:
    """Threat-map filter: only include threats from visible enemies."""
    if ctx.fog_mode == "none":
        return threats
    visible = currently_visible(state, ctx)
    enemy = ctx.team.other()
    visible_enemy_ids = {
        u.id for u in state.units_of(enemy) if u.pos in visible
    }
    raw = threats.get("threats", {})
    out: dict[str, list[str]] = {}
    for key, ids in raw.items():
        kept = [i for i in ids if i in visible_enemy_ids]
        if kept:
            out[key] = kept
    return {"threats": out}


def update_ever_seen(
    state: GameState, team: Team, prior: frozenset[Pos]
) -> frozenset[Pos]:
    """Return prior ∪ currently_visible(team), for classic-mode memory."""
    return prior | visible_tiles(state, team)
