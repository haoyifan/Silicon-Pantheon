"""Pain's Assault on Konoha — Naruto Sage Mode reinforcement at turn 2.

Turn 2: Naruto arrives from Mount Myoboku in Sage Mode at the southern
village gate (7, 11). The one-turn gap at turn 1 lets Kakashi and the
other Leaf ninjas open the scenario with a desperate holding action
before their VIP shows up.

The spawned unit uses unit_id `u_b_naruto_1` so it matches the
scenario's `protect_unit` / `protect_unit_survives` win conditions
(ProtectUnit treats a not-yet-existing VIP as "not-yet-lost", so the
one-turn gap is fine).

Guarded by a once-only flag so repeated on_turn_start invocations are
idempotent.
"""

from __future__ import annotations

from silicon_pantheon.server.engine.state import (
    Pos,
    Team,
    Unit,
    UnitStatus,
)
from silicon_pantheon.server.engine.scenarios import build_unit_stats, find_spawn_pos


# Mirrors the `naruto` unit_classes entry in config.yaml (Sage Mode
# stats). Kept in sync by hand; if you retune one, retune the other.
_NARUTO_SPEC = {
    "display_name": "Naruto Uzumaki",
    "description": (
        "The Nine-Tails Jinchuriki in Sage Mode. Nature energy "
        "amplifies his speed, strength, and senses beyond anything "
        "the Paths of Pain have faced. Rasenshuriken obliterates at "
        "range. VIP -- if he falls, Konoha is lost."
    ),
    "hp_max": 40,
    "atk": 14,
    "defense": 6,
    "res": 8,
    "spd": 8,
    "move": 5,
    "rng_min": 1,
    "rng_max": 2,
    "tags": ["vip"],
    "glyph": "!",
    "color": "bright_yellow",
}

_NARUTO_SPAWN = (7, 11)
_NARUTO_UID = "u_b_naruto_1"


def naruto_reinforcement(state, turn: int, team: str, **_):
    """on_turn_start hook. Spawns Sage Mode Naruto at turn 2 (one-shot)."""
    if turn != 2 or state.__dict__.get("_naruto_arrived"):
        return
    state.__dict__["_naruto_arrived"] = True
    if _NARUTO_UID in state.units:
        return
    x, y = _NARUTO_SPAWN
    stats = build_unit_stats("naruto", _NARUTO_SPEC)
    spawn_pos = find_spawn_pos(state, Pos(x, y))
    state.units[_NARUTO_UID] = Unit(
        id=_NARUTO_UID,
        owner=Team.BLUE,
        class_="naruto",
        pos=spawn_pos,
        hp=stats.hp_max,
        status=UnitStatus.READY,
        stats=stats,
    )
