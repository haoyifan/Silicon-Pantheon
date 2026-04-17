"""Battle of Trost — Eren Titan reinforcement at turn 8.

Turn 8: Eren transforms into the Attack Titan and spawns at (7, 9),
one tile inside the wall breach. He must move to (7, 10) to seal it.

Guarded by a once-only flag so repeated on_turn_start invocations
are idempotent.
"""

from __future__ import annotations

from silicon_pantheon.server.engine.state import (
    Pos,
    Team,
    Unit,
    UnitStatus,
)
from silicon_pantheon.server.engine.scenarios import build_unit_stats


_EREN_TITAN_SPEC = {
    "display_name": "Eren (Attack Titan)",
    "description": (
        "Eren Yeager in Attack Titan form. Fifteen meters of rage and "
        "hardened fists. He must carry the boulder to the breach and "
        "seal Wall Rose — humanity's only hope."
    ),
    "hp_max": 50,
    "atk": 14,
    "defense": 8,
    "res": 2,
    "spd": 4,
    "move": 3,
    "rng_min": 1,
    "rng_max": 1,
    "glyph": "E",
    "color": "bright_yellow",
}

_EREN_SPAWN = (7, 9)


def eren_reinforcement(state, turn: int, team: str, **_):
    """Called every on_turn_start. Spawns Eren Titan on turn 8
    (one-shot)."""
    if turn != 8 or state.__dict__.get("_eren_arrived"):
        return
    state.__dict__["_eren_arrived"] = True
    uid = "u_b_eren_titan_1"
    if uid in state.units:
        return
    x, y = _EREN_SPAWN
    if any(u.pos == Pos(x, y) for u in state.units.values()):
        return
    stats = build_unit_stats("eren_titan", _EREN_TITAN_SPEC)
    state.units[uid] = Unit(
        id=uid,
        owner=Team.BLUE,
        class_="eren_titan",
        pos=Pos(x, y),
        hp=stats.hp_max,
        status=UnitStatus.READY,
        stats=stats,
    )
