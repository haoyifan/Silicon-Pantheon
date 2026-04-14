"""Battle of Five Armies plugins — three scheduled reinforcement waves.

Turn 5:  second_goblin_wave   — 8 more goblin warriors spawn at the
                                northwestern gap.
Turn 15: eagles_arrive        — 4 Great Eagles spawn at the south
                                edge for the alliance.
Turn 18: beorn_arrives        — 1 Beorn (bear form) spawns center-south.

Each guarded by a once-only flag stored on the state so repeated
on_turn_start invocations are idempotent.
"""

from __future__ import annotations

from clash_of_odin.server.engine.state import (
    Pos,
    Team,
    Unit,
    UnitStatus,
)
from clash_of_odin.server.engine.scenarios import _build_unit_stats


def second_goblin_wave(state, turn: int, team: str, **_):
    if turn != 5 or state.__dict__.get("_goblin_wave_2"):
        return
    state.__dict__["_goblin_wave_2"] = True
    spec = _CLASS_SPECS["goblin_warrior"]
    spawns = [
        (1, 9), (2, 9), (3, 8), (4, 8),
        (3, 9), (4, 9), (5, 8), (5, 9),
    ]
    base_idx = sum(1 for u in state.units.values() if u.class_ == "goblin_warrior")
    for i, (x, y) in enumerate(spawns):
        if any(u.pos == Pos(x, y) for u in state.units.values()):
            continue
        idx = base_idx + i + 1
        uid = f"u_r_goblin_warrior_{idx}"
        if uid in state.units:
            continue
        stats = _build_unit_stats("goblin_warrior", spec)
        state.units[uid] = Unit(
            id=uid, owner=Team.RED, class_="goblin_warrior",
            pos=Pos(x, y), hp=stats.hp_max,
            status=UnitStatus.READY, stats=stats,
        )


def eagles_arrive(state, turn: int, team: str, **_):
    if turn != 15 or state.__dict__.get("_eagles_arrived"):
        return
    state.__dict__["_eagles_arrived"] = True
    spec = _CLASS_SPECS["eagle"]
    spawns = [(3, 13), (8, 13), (12, 13), (15, 13)]
    for i, (x, y) in enumerate(spawns, start=1):
        uid = f"u_b_eagle_{i}"
        if uid in state.units:
            continue
        stats = _build_unit_stats("eagle", spec)
        state.units[uid] = Unit(
            id=uid, owner=Team.BLUE, class_="eagle",
            pos=Pos(x, y), hp=stats.hp_max,
            status=UnitStatus.READY, stats=stats,
        )


def beorn_arrives(state, turn: int, team: str, **_):
    if turn != 18 or state.__dict__.get("_beorn_arrived"):
        return
    state.__dict__["_beorn_arrived"] = True
    spec = _CLASS_SPECS["beorn_bear"]
    uid = "u_b_beorn_bear_1"
    if uid in state.units:
        return
    stats = _build_unit_stats("beorn_bear", spec)
    state.units[uid] = Unit(
        id=uid, owner=Team.BLUE, class_="beorn_bear",
        pos=Pos(9, 12), hp=stats.hp_max,
        status=UnitStatus.READY, stats=stats,
    )


# Trimmed copies of the YAML unit_classes specs for the classes the
# plugin spawns. Keeping them here lets the plugin construct stats
# without re-reading config.yaml; kept in sync with the YAML by hand.
_CLASS_SPECS = {
    "goblin_warrior": {
        "hp_max": 14, "atk": 7, "defense": 3, "res": 2,
        "spd": 5, "rng_min": 1, "rng_max": 1, "move": 4,
        "tags": ["goblin"], "sight": 3,
        "glyph": "g", "color": "red",
        "display_name": "Goblin Warrior",
        "description": "Reinforcement from the mountain.",
    },
    "eagle": {
        "hp_max": 26, "atk": 9, "defense": 5, "res": 3,
        "spd": 10, "rng_min": 1, "rng_max": 1, "move": 8,
        "can_enter_mountain": True, "can_enter_forest": True,
        "tags": ["eagle", "flying", "reinforcement"],
        "sight": 6, "glyph": "V", "color": "bright_yellow",
        "display_name": "Great Eagle",
        "description": "Answered Gwaihir's call.",
    },
    "beorn_bear": {
        "hp_max": 48, "atk": 16, "defense": 8, "res": 3,
        "spd": 7, "rng_min": 1, "rng_max": 1, "move": 5,
        "tags": ["bear", "hero", "reinforcement"],
        "sight": 4, "glyph": "R", "color": "bright_yellow",
        "display_name": "Beorn (bear form)",
        "description": "Skin-changer of the Carrock.",
    },
}
