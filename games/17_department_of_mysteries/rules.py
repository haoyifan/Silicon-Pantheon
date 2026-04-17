"""Department of Mysteries plugin — Order of the Phoenix reinforcements.

Turn 10: order_reinforcements — 4 Order members (Sirius, Lupin, Tonks,
                                 Moody) spawn at the corridor entrances
                                 for blue.

Guarded by a once-only flag stored on the state so repeated
on_turn_start invocations are idempotent.
"""

from __future__ import annotations

from silicon_pantheon.server.engine.state import (
    Pos,
    Team,
    Unit,
    UnitStatus,
)
from silicon_pantheon.server.engine.scenarios import build_unit_stats


def order_reinforcements(state, turn: int, team: str, **_):
    """Turn 10: the Order of the Phoenix arrives with 4 reinforcements."""
    if turn != 10 or state.__dict__.get("_order_arrived"):
        return
    state.__dict__["_order_arrived"] = True

    spawns = [
        ("sirius", 4, 6),
        ("lupin", 9, 6),
        ("tonks", 0, 4),
        ("moody", 0, 3),
    ]
    for class_name, x, y in spawns:
        uid = f"u_b_{class_name}_1"
        if uid in state.units:
            continue
        pos = Pos(x, y)
        if any(u.pos == pos for u in state.units.values()):
            continue
        spec = _CLASS_SPECS[class_name]
        stats = build_unit_stats(class_name, spec)
        state.units[uid] = Unit(
            id=uid, owner=Team.BLUE, class_=class_name,
            pos=pos, hp=stats.hp_max,
            status=UnitStatus.READY, stats=stats,
        )


_CLASS_SPECS = {
    "sirius": {
        "hp_max": 26, "atk": 11, "defense": 4, "res": 6,
        "spd": 6, "rng_min": 1, "rng_max": 2, "move": 4,
        "is_magic": True,
        "glyph": "S", "color": "white",
        "display_name": "Sirius Black",
        "description": "Harry's godfather. Fights with reckless joy.",
    },
    "lupin": {
        "hp_max": 24, "atk": 10, "defense": 4, "res": 6,
        "spd": 5, "rng_min": 1, "rng_max": 2, "move": 3,
        "is_magic": True,
        "glyph": "r", "color": "yellow",
        "display_name": "Remus Lupin",
        "description": "Werewolf, former professor, precise duelist.",
    },
    "tonks": {
        "hp_max": 22, "atk": 10, "defense": 3, "res": 5,
        "spd": 6, "rng_min": 1, "rng_max": 2, "move": 4,
        "is_magic": True,
        "glyph": "T", "color": "magenta",
        "display_name": "Nymphadora Tonks",
        "description": "Metamorphmagus Auror. Clumsy off-duty, lethal on.",
    },
    "moody": {
        "hp_max": 28, "atk": 11, "defense": 5, "res": 7,
        "spd": 4, "rng_min": 1, "rng_max": 3, "move": 3,
        "is_magic": True,
        "glyph": "E", "color": "bright_yellow",
        "display_name": "Alastor 'Mad-Eye' Moody",
        "description": "Retired Auror. His magical eye sees through walls.",
    },
}
