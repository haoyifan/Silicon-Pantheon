"""Blackwater Bay — Tyrell reinforcement at turn 12.

Turn 12: four Tyrell knights spawn at the eastern edge for red.
Tywin Lannister and the Tyrells arrive to save King's Landing.

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


_TYRELL_KNIGHT_SPEC = {
    "display_name": "Tyrell Knight",
    "description": (
        "Heavy cavalry of Highgarden, arriving with Tywin Lannister. "
        "Fresh, armored, and devastating."
    ),
    "hp_max": 26,
    "atk": 10,
    "defense": 7,
    "res": 3,
    "spd": 6,
    "move": 5,
    "rng_min": 1,
    "rng_max": 1,
    "glyph": "R",
    "color": "bright_red",
}

_TYRELL_SPAWNS = [
    (13, 2), (13, 8), (12, 1), (12, 9),
]


def tyrell_reinforcements(state, turn: int, team: str, **_):
    """Called every on_turn_start. Spawns Tyrell knights on
    turn 12 (one-shot)."""
    if turn != 12 or state.__dict__.get("_tyrell_arrived"):
        return
    state.__dict__["_tyrell_arrived"] = True
    for i, (x, y) in enumerate(_TYRELL_SPAWNS, start=1):
        uid = f"u_r_tyrell_knight_{i}"
        if uid in state.units:
            continue
        if any(u.pos == Pos(x, y) for u in state.units.values()):
            continue
        stats = build_unit_stats("tyrell_knight", _TYRELL_KNIGHT_SPEC)
        state.units[uid] = Unit(
            id=uid,
            owner=Team.RED,
            class_="tyrell_knight",
            pos=Pos(x, y),
            hp=stats.hp_max,
            status=UnitStatus.READY,
            stats=stats,
        )
