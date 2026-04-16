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
    "Blue (US + Israel) has two objectives and must complete BOTH "
    "within 10 turns to win:\n"
    "  1) Kill Khamenei (any blue unit kills the red 'khamenei' unit).\n"
    f"  2) Capture the enriched-uranium bunker — any blue unit must "
    f"end a turn standing on tile ({_URANIUM_BUNKER_POS.x}, "
    f"{_URANIUM_BUNKER_POS.y}).\n"
    "Doing only one of the two does not win — blue has to do both.\n"
    "Red (Iran) wins in any of these cases: 10 turns pass without "
    "blue finishing both objectives; either of blue's two leaders "
    "(Trump or Netanyahu) is killed; or blue runs out of units."
)


def enriched_uranium_strike_check_describe(state, viewer, **_):
    """Companion to enriched_uranium_strike_check — called by
    get_tactical_summary's win-progress renderer when it looks for
    `<check_fn>_describe` in the plugin namespace.

    Returns a viewer-relative one-liner showing objective status +
    turn countdown.
    """
    dead = getattr(state, "dead_unit_ids", set())
    khamenei_dead = _KHAMENEI_ID in dead
    blue_on_bunker = any(
        u.pos == _URANIUM_BUNKER_POS for u in state.units_of(Team.BLUE)
    )
    remaining = max(0, state.max_turns - state.turn + 1)

    obj1 = "✓ killed" if khamenei_dead else "✗ alive"
    obj2 = "✓ occupied" if blue_on_bunker else "✗ not reached"

    # Closest blue unit to the bunker (useful when not yet occupied).
    blue_units = [u for u in state.units_of(Team.BLUE) if u.alive]
    bunker_dist = ""
    if not blue_on_bunker and blue_units:
        closest = min(blue_units, key=lambda u: u.pos.manhattan(_URANIUM_BUNKER_POS))
        d = closest.pos.manhattan(_URANIUM_BUNKER_POS)
        bunker_dist = f" (closest: {closest.id}, {d} tile(s) away)"

    if viewer is Team.BLUE:
        return (
            f"Operation Epic Fury: Khamenei {obj1} / bunker at "
            f"({_URANIUM_BUNKER_POS.x},{_URANIUM_BUNKER_POS.y}) {obj2}"
            f"{bunker_dist}. MUST complete BOTH within "
            f"{remaining} turn(s) or Iran wins."
        )
    return (
        f"Hold the line: Khamenei {obj1} / bunker {obj2}. "
        f"Survive {remaining} more turn(s) without blue completing "
        f"both objectives and you win."
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
