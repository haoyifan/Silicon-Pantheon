"""Plugins for the Strait of Hormuz scenario (2026 Iran war).

Two callables:

  - enriched_uranium_strike_check: the authoritative win rule for
    this scenario. See its docstring for the precise compound
    condition.

  - sea_mine_effect: the contact-mine terrain hook. Any unit ending
    its turn on a sea_mine tile takes 6 HP. One-shot — the mine
    detonates and the tile flips to plain water.
"""

from __future__ import annotations

from silicon_pantheon.server.engine.state import Pos, Team, Tile
from silicon_pantheon.server.engine.win_conditions.base import WinResult

# Coordinates of the enriched-uranium bunker must match config.yaml.
# The tile uses terrain_type "uranium_bunker" and is the goal tile
# blue must occupy for the strike package to count as delivered.
_URANIUM_BUNKER_POS = Pos(15, 5)
_KHAMENEI_ID = "u_r_khamenei_1"


def enriched_uranium_strike_check(state, hook: str, **_):
    """Operation Epic Fury compound win rule.

    Blue (US + Israel) is running a joint strike package with two
    objectives that BOTH must land inside the ten-turn window:

        1. Assassinate Ayatollah Khamenei (mirrors the real strike
           in Tehran on 2026-02-28 that killed him day 1).
        2. Plant boots on the Iranian enriched-uranium bunker
           (terrain tile "uranium_bunker" at position %s).

    Exactly one of those two isn't enough — bombing the bunker
    without decapitating the regime leaves Iran capable of
    resupply; killing the Leader without reaching the enrichment
    site means the program survives. Blue must stack both.

    Red (Iran) wins if the ten-turn budget expires with the
    compound goal unmet, OR if either US/Israeli leader is killed
    (the protect_unit rules in config.yaml handle that case
    directly).
    """
    if hook != "end_turn":
        return None

    dead = getattr(state, "dead_unit_ids", set())
    khamenei_dead = _KHAMENEI_ID in dead

    # Blue victory lane: both objectives delivered this turn or earlier.
    blue_on_bunker = any(
        u.pos == _URANIUM_BUNKER_POS for u in state.units_of(Team.BLUE)
    )
    if blue_on_bunker and khamenei_dead:
        return WinResult(
            winner="blue",
            reason="enriched_uranium_seized_and_khamenei_killed",
            details={
                "uranium_bunker": _URANIUM_BUNKER_POS.to_dict(),
                "khamenei_id": _KHAMENEI_ID,
            },
        )

    # Red victory lane: Iran holds the line for the full ten turns.
    # state.turn increments when play wraps back to first_player,
    # so after blue's and red's turn-10 halves state.turn == 11.
    if state.turn > state.max_turns:
        return WinResult(
            winner="red",
            reason="iran_held_the_line",
            details={"khamenei_dead": khamenei_dead},
        )

    return None


# Docstring can't substitute %s at class-load time; patch post-def.
enriched_uranium_strike_check.__doc__ = (
    enriched_uranium_strike_check.__doc__ or ""
).replace("%s", f"({_URANIUM_BUNKER_POS.x}, {_URANIUM_BUNKER_POS.y})")


# Human-readable description surfaced in the room preview + the
# system prompt passed to agents. Server-side `describe_scenario`
# looks for this attribute (falling back to the docstring's first
# line) so the player/agent never has to decode the function name.
enriched_uranium_strike_check.description = (
    "Blue (US+Israel) wins only by delivering BOTH halves of the "
    f"strike package within {10} turns: "
    f"reach the enriched-uranium bunker at "
    f"({_URANIUM_BUNKER_POS.x}, {_URANIUM_BUNKER_POS.y}) AND "
    "kill Khamenei. Either objective alone is not enough. Red "
    "(Iran) wins if the ten-turn budget expires with the compound "
    "goal unmet, or if either US/Israeli leader dies."
)


def sea_mine_effect(state, unit, tile, hook: str, **_):
    """Terrain effect for sea_mine tiles.

    Fires at end_turn for whichever team just ended. Any unit
    standing on a mine takes 6 damage. The mine then detonates
    (one-shot) and the tile flips to plain water so the next
    unit to cross gets a clear lane.
    """
    if hook != "end_turn":
        return {"hp_delta": 0}
    # Detonate exactly once: replace the tile with water before the
    # rules-engine's hp_delta application so the damage is the
    # mine's final word.
    pos = unit.pos
    state.board.tiles[pos] = Tile(
        pos=pos,
        type="water",
        passable=True,
        glyph="~",
        color="blue",
    )
    return {"hp_delta": -6}
