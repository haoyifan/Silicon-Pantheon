"""Produce the `get_state` JSON payload from a GameState."""

from __future__ import annotations

from .state import GameState, Pos, Team


def state_to_dict(state: GameState, viewer: Team | None = None) -> dict:
    """Serialize state to the dict shape documented in GAME_DESIGN.md.

    `viewer` is reserved for future fog-of-war support; currently ignored
    (full state is returned regardless).
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
            tiles.append({"x": x, "y": y, "type": tile.type.value})

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
    for u in state.units.values():
        if not u.alive:
            continue
        units_payload.append(
            {
                "id": u.id,
                "owner": u.owner.value,
                "class": u.class_.value,
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
    }
