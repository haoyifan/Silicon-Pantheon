"""Battle of Trost — Eren reinforcement at turn 4.

Turn 4: Eren transforms into the Attack Titan and spawns just inside
the breach. He must move to the breach tile at y=10 to seal Wall Rose.

Spawns a unit of class ``eren`` (matches the class defined in
``config.yaml``) with UID ``u_b_eren_1`` — this UID is referenced by
the ``reach_tile``/``protect_unit`` win conditions and by the
``on_unit_killed`` narrative event.

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


# Mirrors `unit_classes.eren` in config.yaml so the spawned titan has
# the same combat profile as the declared class.
_EREN_SPEC = {
    "display_name": "Eren Yeager (Attack Titan)",
    "description": (
        "Eren in Attack Titan form. Fifteen meters of rage and "
        "hardened fists. VIP — he must carry the boulder to the "
        "breach at y=10 and seal Wall Rose."
    ),
    "hp_max": 40,
    "atk": 13,
    "defense": 7,
    "res": 3,
    "spd": 4,
    "move": 3,
    "rng_min": 1,
    "rng_max": 1,
    "tags": ["vip"],
    "glyph": "E",
    "color": "bright_yellow",
}

_EREN_SPAWN = (5, 2)
_EREN_TURN = 4


def eren_reinforcement(state, turn: int, team: str, **_):
    """Called every on_turn_start. Spawns Eren on turn 4 (one-shot)."""
    if turn != _EREN_TURN or state.__dict__.get("_eren_arrived"):
        return
    state.__dict__["_eren_arrived"] = True
    uid = "u_b_eren_1"
    if uid in state.units:
        return
    x, y = _EREN_SPAWN
    stats = build_unit_stats("eren", _EREN_SPEC)
    spawn_pos = find_spawn_pos(state, Pos(x, y))
    state.units[uid] = Unit(
        id=uid,
        owner=Team.BLUE,
        class_="eren",
        pos=spawn_pos,
        hp=stats.hp_max,
        status=UnitStatus.READY,
        stats=stats,
    )
