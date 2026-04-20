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

from silicon_pantheon.server.engine.serialize import state_to_dict
from silicon_pantheon.server.engine.state import GameState, Pos, Team, TerrainType
from silicon_pantheon.shared.fog import visible_tiles

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


def _action_is_visible(ev: dict, state: GameState, ctx: ViewerContext) -> bool:
    """Check if an action event should be visible under fog-of-war.
    
    Returns True if action should be shown, False if it should be redacted.
    - Own-team actions always show
    - Position-free events (end_turn) always show
    - Enemy actions show only if actor is currently visible OR destination tile is visible
    """
    if not isinstance(ev, dict):
        return True
    
    t = ev.get("type")
    # Own actions always surface.
    actor_id = ev.get("unit_id") or ev.get("by")
    team_char = ctx.team.value[0]  # 'b' for blue, 'r' for red
    
    if isinstance(actor_id, str) and actor_id.startswith(f"u_{team_char}_"):
        return True
    if ev.get("by") == ctx.team.value:
        return True
    # Position-free events (end_turn, etc.) pass through.
    if t == "end_turn":
        return True
    # Enemy action: show only if actor currently visible OR the
    # referenced tile is visible. "Currently visible" is a proxy
    # for "the player saw this happen"; imperfect but doesn't
    # leak fresh intel.
    visible = currently_visible(state, ctx)
    enemy_ids_currently_visible = {
        u.id for u in state.units_of(ctx.team.other()) if u.pos in visible
    }
    
    if isinstance(actor_id, str) and actor_id in enemy_ids_currently_visible:
        return True
    dest = ev.get("dest")
    if isinstance(dest, dict):
        from silicon_pantheon.server.engine.state import Pos as _Pos
        if _Pos(int(dest.get("x", -1)), int(dest.get("y", -1))) in visible:
            return True
    return False


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
    
    # Redact last_action if it references a hidden enemy.
    la = raw.get("last_action")
    if isinstance(la, dict) and not _action_is_visible(la, state, ctx):
        raw["last_action"] = None
    
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


def filter_history(
    history_result: dict[str, Any], state: GameState, ctx: ViewerContext
) -> dict[str, Any]:
    """Filter action history so fog-of-war doesn't leak through.

    The raw history records every action by both sides verbatim,
    including enemy unit ids and destination tiles. Under fog modes
    a viewer should only see enemy actions whose relevant position
    was visible WHEN the action happened — but that per-turn
    visibility isn't preserved in the record, so we apply the
    conservative rule: show an enemy action only if the tile it
    references is currently visible.

    Own-team actions always pass through (you saw yourself act).
    end_turn events are surfaced verbatim — they carry no position.

    Concretely this stops an agent from reading "u_r_stealth_1 moved
    to (10, 4)" in the delta prompt when that unit is otherwise
    hidden under fog.
    """
    if ctx.fog_mode == "none":
        return history_result

    filtered = [ev for ev in (history_result.get("history") or []) if _action_is_visible(ev, state, ctx)]
    out = dict(history_result)
    out["history"] = filtered
    # last_action: redact if not visible under the same rule.
    la = out.get("last_action")
    if isinstance(la, dict) and not _action_is_visible(la, state, ctx):
        out["last_action"] = None
    return out


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
