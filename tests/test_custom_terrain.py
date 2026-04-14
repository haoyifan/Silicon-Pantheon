"""Tests for scenario terrain_types: block — custom terrain effects."""

from __future__ import annotations

from clash_of_odin.server.engine.rules import EndTurnAction, apply
from clash_of_odin.server.engine.scenarios import build_state


def _cfg(
    *,
    terrain_types: dict | None = None,
    terrain: list | None = None,
    blue=None,
    red=None,
    unit_classes: dict | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "terrain_types": terrain_types or {},
        "unit_classes": unit_classes or {},
        "board": {
            "width": 5,
            "height": 5,
            "terrain": terrain or [],
            "forts": [],
        },
        "armies": {
            "blue": blue or [{"class": "knight", "pos": {"x": 0, "y": 0}}],
            "red": red or [{"class": "knight", "pos": {"x": 4, "y": 4}}],
        },
        "rules": {"max_turns": 10, "first_player": "blue"},
    }


def test_custom_terrain_movement_cost_honored() -> None:
    state = build_state(
        _cfg(
            terrain_types={"sand": {"move_cost": 3, "passable": True}},
            terrain=[{"x": 1, "y": 0, "type": "sand"}],
        )
    )
    tile = state.board.tile_at = state.board.tile  # alias for readability
    sand = state.board.tile(__import__("clash_of_odin.server.engine.state", fromlist=["Pos"]).Pos(1, 0))
    assert sand.move_cost() == 3
    assert sand.passable is True


def test_custom_terrain_damage_tile_hurts_unit_on_end_turn() -> None:
    # Blue knight at (2,2) standing on lava; red knight far away.
    # Blue ends turn -> heals=-5 applies.
    state = build_state(
        _cfg(
            terrain_types={"lava": {"heals": -5, "passable": True}},
            terrain=[{"x": 2, "y": 2, "type": "lava"}],
            blue=[{"class": "knight", "pos": {"x": 2, "y": 2}}],
        )
    )
    blue = state.units["u_b_knight_1"]
    hp_before = blue.hp
    apply(state, EndTurnAction())
    # End-of-turn damage applied to blue (same team got the fort-heal
    # reset pass + lava damage).
    assert blue.hp == hp_before - 5


def test_custom_terrain_heal_tile_revives_hp() -> None:
    state = build_state(
        _cfg(
            terrain_types={"spring": {"heals": 3, "passable": True}},
            terrain=[{"x": 2, "y": 2, "type": "spring"}],
            blue=[{"class": "knight", "pos": {"x": 2, "y": 2}}],
        )
    )
    blue = state.units["u_b_knight_1"]
    blue.hp = 10  # wounded
    apply(state, EndTurnAction())
    assert blue.hp == 13


def test_custom_terrain_blocks_sight() -> None:
    from clash_of_odin.server.engine.state import Pos, Team
    from clash_of_odin.shared.fog import visible_tiles

    # Wall tile at (1,0); blue archer at (0,0) with sight 4; red
    # knight at (2,0). Under LOS rules the wall should mask (2,0).
    state = build_state(
        _cfg(
            terrain_types={"wall": {"blocks_sight": True, "passable": True}},
            terrain=[{"x": 1, "y": 0, "type": "wall"}],
            blue=[{"class": "archer", "pos": {"x": 0, "y": 0}}],
            red=[{"class": "knight", "pos": {"x": 2, "y": 0}}],
        )
    )
    vis = visible_tiles(state, Team.BLUE)
    # Own tile and adjacent always visible regardless of terrain.
    assert Pos(0, 0) in vis
    # (2,0) is blocked from (0,0) by the wall at (1,0), not adjacent.
    assert Pos(2, 0) not in vis


def test_class_override_prevents_passage() -> None:
    from clash_of_odin.server.engine.board import can_enter
    from clash_of_odin.server.engine.state import Pos

    state = build_state(
        _cfg(
            terrain_types={
                "sand": {
                    "passable": True,
                    "class_overrides": {"cavalry": {"passable": False}},
                }
            },
            terrain=[{"x": 1, "y": 0, "type": "sand"}],
            blue=[{"class": "cavalry", "pos": {"x": 0, "y": 0}}],
        )
    )
    cavalry = state.units["u_b_cavalry_1"]
    sand_tile = state.board.tile(Pos(1, 0))
    assert not can_enter(cavalry.stats, sand_tile, cavalry.class_)
    # But a knight can cross it (no override).
    from clash_of_odin.server.engine.units import make_stats
    from clash_of_odin.server.engine.state import UnitClass

    knight_stats = make_stats(UnitClass.KNIGHT)
    assert can_enter(knight_stats, sand_tile, "knight")


def test_builtin_terrain_still_works() -> None:
    """No custom terrain block — built-in forest / mountain / fort
    behave like before."""
    state = build_state(
        _cfg(terrain=[{"x": 1, "y": 1, "type": "forest"}])
    )
    from clash_of_odin.server.engine.state import Pos

    tile = state.board.tile(Pos(1, 1))
    assert tile.move_cost() == 2  # legacy forest cost
    assert tile.def_bonus() == 2  # legacy forest DEF bonus
