"""Pelennor Fields — Rohan reinforcement at turn 8.

Turn 8: six Rohirrim cavalry spawn at the south edge for blue.
The Riders of Rohan answer Gondor's call.

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


_ROHIRRIM_SPEC = {
    "display_name": "Rohirrim",
    "description": (
        "Riders of Rohan, answering Gondor's call. Fast cavalry "
        "that charges with devastating force."
    ),
    "hp_max": 22,
    "atk": 10,
    "defense": 5,
    "res": 3,
    "spd": 7,
    "move": 5,
    "rng_min": 1,
    "rng_max": 1,
    "glyph": "R",
    "color": "bright_cyan",
}

# Spawn positions — south edge of the 18x12 map, spread across.
_ROHIRRIM_SPAWNS = [
    (4, 11), (6, 11), (8, 11), (10, 11), (12, 11), (14, 11),
]


def rohan_reinforcements(state, turn: int, team: str, **_):
    """Called every on_turn_start. Spawns Rohirrim cavalry on
    turn 8 (one-shot)."""
    if turn != 8 or state.__dict__.get("_rohan_arrived"):
        return
    state.__dict__["_rohan_arrived"] = True
    for i, (x, y) in enumerate(_ROHIRRIM_SPAWNS, start=1):
        uid = f"u_b_rohirrim_{i}"
        if uid in state.units:
            continue
        if any(u.pos == Pos(x, y) for u in state.units.values()):
            continue
        stats = build_unit_stats("rohirrim", _ROHIRRIM_SPEC)
        state.units[uid] = Unit(
            id=uid,
            owner=Team.BLUE,
            class_="rohirrim",
            pos=Pos(x, y),
            hp=stats.hp_max,
            status=UnitStatus.READY,
            stats=stats,
        )
