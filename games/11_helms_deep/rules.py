"""Helm's Deep plugins.

Two scheduled events:

  - Turn 4: explode_culvert. The Deeping Wall gate at (4, 5)
    becomes a passable plain tile. The wall is breached and
    uruks can pour through.

  - Turn 12: gandalf_arrives. Gandalf, Éomer, and a band of
    Rohirrim cavalry spawn at the east edge of the map (x=15)
    on the blue side, ready to charge the uruk rear.

Both events fire once and only once. Repeated calls (the engine
calls on_turn_start hooks every end_turn) are no-ops via guard
flags stored on the state.
"""

from __future__ import annotations

from clash_of_odin.server.engine.state import (
    Pos,
    Team,
    Tile,
    Unit,
    UnitStatus,
)
from clash_of_odin.server.engine.units import make_stats
from clash_of_odin.server.engine.scenarios import _build_unit_stats


def explode_culvert(state, turn: int, team: str, **_):
    """Demolish the Deeping Gate at (4, 5) on turn 4."""
    if turn != 4:
        return
    if state.__dict__.get("_culvert_exploded"):
        return
    state.__dict__["_culvert_exploded"] = True
    # Replace the gate tile with rubble — passable, no defense bonus,
    # so the uruks can stream through. Keep the glyph as a debris
    # marker so the breach is visible on the map.
    pos = Pos(4, 5)
    state.board.tiles[pos] = Tile(
        pos=pos,
        type="rubble",
        passable=True,
        glyph=".",
        color="bright_black",
    )


def gandalf_arrives(state, turn: int, team: str, **_):
    """Spawn the dawn reinforcements at turn 12."""
    if turn != 12:
        return
    if state.__dict__.get("_gandalf_arrived"):
        return
    state.__dict__["_gandalf_arrived"] = True

    # The reinforcement classes are scenario-defined; we look them
    # up via the state's class table by re-reading describe_scenario
    # data from the config. Instead, just construct stats from a
    # base built-in and override fields. Easier: pull stats from any
    # alive unit of the same class if present, else use defaults.
    spawns = [
        ("gandalf", Pos(15, 4)),
        ("eomer", Pos(15, 5)),
        ("rohirrim_cavalry", Pos(15, 3)),
        ("rohirrim_cavalry", Pos(15, 6)),
        ("rohirrim_cavalry", Pos(14, 3)),
        ("rohirrim_cavalry", Pos(14, 6)),
    ]

    # Find a template stats object by class name from the scenario's
    # _class_table. The scenario loader doesn't expose it, but it
    # does store classes used by initial armies — and these aren't
    # in the initial army. So we rebuild from the config.yaml dict.
    # Use a copy-from-spec helper we ship inline:
    cfg = _scenario_unit_classes()
    name_to_idx: dict[str, int] = {}
    for cname, _pos in spawns:
        spec = cfg.get(cname)
        if spec is None:
            continue
        idx = name_to_idx.get(cname, 0) + 1
        name_to_idx[cname] = idx
        uid = f"u_b_{cname}_{idx}"
        # Skip if a unit with that id already exists (replay safety).
        if uid in state.units:
            continue
        stats = _build_unit_stats(cname, spec)
        state.units[uid] = Unit(
            id=uid,
            owner=Team.BLUE,
            class_=cname,
            pos=_pos,
            hp=stats.hp_max,
            status=UnitStatus.READY,
            stats=stats,
        )


def _scenario_unit_classes() -> dict:
    """Return the unit_classes spec dict for the reinforcement
    classes. Hardcoded here so the plugin doesn't need to re-parse
    config.yaml at runtime — kept in sync with the YAML manually."""
    return {
        "gandalf": {
            "hp_max": 50, "atk": 14, "defense": 6, "res": 8,
            "spd": 8, "rng_min": 1, "rng_max": 2, "move": 6,
            "is_magic": True, "can_enter_mountain": True,
            "tags": ["hero", "wizard", "reinforcement"],
            "sight": 6, "glyph": "W", "color": "bright_yellow",
            "display_name": "Gandalf the White",
            "description": "Arrived at dawn with Éomer's cavalry.",
        },
        "eomer": {
            "hp_max": 32, "atk": 11, "defense": 6, "res": 3,
            "spd": 8, "rng_min": 1, "rng_max": 1, "move": 6,
            "tags": ["hero", "cavalry", "reinforcement"],
            "sight": 4, "glyph": "O", "color": "bright_yellow",
            "display_name": "Éomer",
            "description": "Marshal of the East-mark.",
        },
        "rohirrim_cavalry": {
            "hp_max": 26, "atk": 10, "defense": 4, "res": 3,
            "spd": 8, "rng_min": 1, "rng_max": 1, "move": 6,
            "tags": ["cavalry", "rohan", "reinforcement"],
            "sight": 4, "glyph": "R", "color": "bright_yellow",
            "display_name": "Rohirrim Cavalry",
            "description": "Mounted warriors of the Mark.",
        },
    }
