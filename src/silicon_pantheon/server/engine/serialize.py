"""Produce the `get_state` JSON payload from a GameState."""

from __future__ import annotations

from .state import GameState, Pos, Team


def state_to_dict(
    state: GameState,
    viewer: Team | None = None,
    *,
    fog_of_war: str | None = None,
) -> dict:
    """Serialize state to the dict shape documented in GAME_DESIGN.md.

    ``viewer`` is reserved for future fog-of-war support at this
    layer; today the raw dict returned here is the FULL state and
    fog filtering is applied separately by ``filter_state`` in the
    dispatch layer (see ``server/game_tools.py::_apply_filter``).

    ``fog_of_war`` is passed through to the response so agents can
    read the effective fog mode from any ``get_state`` call and
    reason correctly about what ``units`` does or doesn't contain.
    Previously agents had no way to tell whether the unit list was
    fog-filtered — they'd look at their scenario config (which no
    longer records fog mode), guess wrong, and file state-
    inconsistency bug reports when ``get_state``'s filtered unit
    count disagreed with ``win_progress``'s unfiltered one. Room is
    the single source of truth.
    """
    # Flat list of {x, y, type} — matches what the viewer-filter
    # writes when masking for fog, and what the TUI expects. Earlier
    # versions emitted a nested list under the key "terrain", which
    # nothing in the current codebase reads — the old consumers
    # worked around it by only looking at units/forts, and the TUI
    # would fall through to "unknown" for every cell (rendered as "?").
    tiles = []
    for y in range(state.board.height):
        for x in range(state.board.width):
            tile = state.board.tile(Pos(x, y))
            tiles.append({"x": x, "y": y, "type": tile.type})

    forts = []
    for pos, tile in state.board.tiles.items():
        if tile.is_fort:
            forts.append(
                {
                    "x": pos.x,
                    "y": pos.y,
                    "owner": tile.fort_owner.value if tile.fort_owner else None,
                }
            )

    units_payload = []
    # Serialize alive units from state.units, then append fallen-unit
    # snapshots so clients can still render them dim in the roster.
    # Engine invariants (`units_of`, position lookups) operate on
    # `state.units` only — these ghosts never rejoin that dict.
    for u in list(state.units.values()) + list(state.fallen_units.values()):
        # Include dead units (hp=0) so clients can show them dim in
        # the units table without losing the record; the `alive` flag
        # lets the client skip them on the board itself.
        units_payload.append(
            {
                "id": u.id,
                "owner": u.owner.value,
                "class": u.class_,
                "pos": u.pos.to_dict(),
                "hp": u.hp,
                "hp_max": u.stats.hp_max,
                "atk": u.stats.atk,
                "def": u.stats.defense,
                "res": u.stats.res,
                "spd": u.stats.spd,
                "rng": [u.stats.rng_min, u.stats.rng_max],
                "move": u.stats.move,
                "is_magic": u.stats.is_magic,
                "can_heal": u.stats.can_heal,
                "status": u.status.value,
                "alive": u.alive,
                # Reserved v2 fields — agents / clients can read them
                # today even though the engine doesn't act on them yet.
                "tags": list(u.stats.tags),
                "mp_max": u.stats.mp_max,
                "mp_per_turn": u.stats.mp_per_turn,
                "abilities": list(u.stats.abilities),
                "default_inventory": list(u.stats.default_inventory),
                "damage_profile": dict(u.stats.damage_profile),
                "defense_profile": dict(u.stats.defense_profile),
                "bonus_vs_tags": [dict(b) for b in u.stats.bonus_vs_tags],
                "vulnerability_to_tags": [
                    dict(v) for v in u.stats.vulnerability_to_tags
                ],
                "glyph": u.stats.glyph,
                "color": u.stats.color,
                "display_name": u.stats.display_name,
                "description": u.stats.description,
                "art_frames": list(u.stats.art_frames),
            }
        )

    return {
        "game_id": state.game_id,
        "turn": state.turn,
        "max_turns": state.max_turns,
        "active_player": state.active_player.value,
        "you": viewer.value if viewer else None,
        "first_player": state.first_player.value,
        "status": state.status.value,
        "winner": state.winner.value if state.winner else None,
        # Effective fog mode for this match (read from the session,
        # not from the scenario). See docstring.
        "fog_of_war": fog_of_war,
        "board": {
            "width": state.board.width,
            "height": state.board.height,
            "tiles": tiles,
            "forts": forts,
        },
        "units": units_payload,
        "turn_clock": {
            "turns_remaining": max(0, state.max_turns - state.turn + 1),
            "max_turns": state.max_turns,
        },
        "last_action": state.last_action,
        "prev_turn_actions": list(state.prev_turn_actions),
    }
