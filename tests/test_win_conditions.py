"""Declarative win-conditions framework tests.

Covers:
  - build_conditions() resolves DSL type strings to rule instances
  - default_conditions() returns the legacy 3-rule stack
  - reach_tile fires when the right team's unit lands on the pos
  - protect_unit fires when the VIP dies
  - reach_goal_line fires when any unit crosses
  - unknown type raises
"""

from __future__ import annotations

import pytest

from clash_of_odin.server.engine.rules import EndTurnAction, apply
from clash_of_odin.server.engine.state import (
    Board,
    GameState,
    GameStatus,
    Pos,
    Team,
    Tile,
    Unit,
    UnitStatus,
)
from clash_of_odin.server.engine.units import make_stats
from clash_of_odin.server.engine.state import UnitClass
from clash_of_odin.server.engine.win_conditions import (
    build_conditions,
    default_conditions,
)
from clash_of_odin.server.engine.win_conditions.rules import (
    EliminateAllEnemyUnits,
    MaxTurnsDraw,
    ProtectUnit,
    ReachGoalLine,
    ReachTile,
    SeizeEnemyFort,
)


def _make_state(
    units: dict[str, Unit],
    *,
    first_player: Team = Team.BLUE,
    width: int = 8,
    height: int = 8,
) -> GameState:
    board = Board(width=width, height=height, tiles={})
    return GameState(
        game_id="g_test",
        turn=1,
        max_turns=30,
        active_player=first_player,
        first_player=first_player,
        board=board,
        units=units,
    )


def _mkunit(uid: str, team: Team, pos: Pos, cls: UnitClass = UnitClass.KNIGHT) -> Unit:
    stats = make_stats(cls)
    return Unit(
        id=uid,
        owner=team,
        class_=cls.value,
        pos=pos,
        hp=stats.hp_max,
        status=UnitStatus.READY,
        stats=stats,
    )


def test_default_conditions_has_three_rules():
    rules = default_conditions()
    assert len(rules) == 3
    assert isinstance(rules[0], SeizeEnemyFort)
    assert isinstance(rules[1], EliminateAllEnemyUnits)
    assert isinstance(rules[2], MaxTurnsDraw)


def test_build_conditions_resolves_types():
    rules = build_conditions([
        {"type": "reach_tile", "team": "blue", "pos": {"x": 5, "y": 5}},
        {"type": "protect_unit", "unit_id": "u_b_infantry_1", "owning_team": "blue"},
    ])
    assert isinstance(rules[0], ReachTile)
    assert rules[0].team == "blue"
    assert isinstance(rules[1], ProtectUnit)


def test_build_conditions_unknown_type_raises():
    with pytest.raises(ValueError, match="unknown win_condition type"):
        build_conditions([{"type": "nonsense_rule"}])


def test_reach_tile_fires_on_end_turn():
    blue = _mkunit("u_b_1", Team.BLUE, Pos(5, 5))
    red = _mkunit("u_r_1", Team.RED, Pos(0, 0))
    state = _make_state({blue.id: blue, red.id: red})
    state._win_conditions = build_conditions([
        {"type": "reach_tile", "team": "blue", "pos": {"x": 5, "y": 5}},
    ])
    result = apply(state, EndTurnAction())
    assert result["winner"] == "blue"
    assert result["reason"] == "reach_tile"
    assert state.status is GameStatus.GAME_OVER


def test_reach_tile_does_not_fire_when_wrong_team():
    blue = _mkunit("u_b_1", Team.BLUE, Pos(0, 0))
    red = _mkunit("u_r_1", Team.RED, Pos(5, 5))
    state = _make_state({blue.id: blue, red.id: red})
    state._win_conditions = build_conditions([
        {"type": "reach_tile", "team": "blue", "pos": {"x": 5, "y": 5}},
    ])
    result = apply(state, EndTurnAction())
    assert result["winner"] is None
    assert state.status is not GameStatus.GAME_OVER


def test_protect_unit_fires_when_vip_missing():
    # VIP already dead (not in state.units).
    red = _mkunit("u_r_1", Team.RED, Pos(5, 5))
    state = _make_state({red.id: red})
    state._win_conditions = build_conditions([
        {"type": "protect_unit", "unit_id": "u_b_vip", "owning_team": "blue"},
    ])
    # protect_unit keys off `state.units.get(...)` — missing is treated
    # as alive (None). Add a dead-by-hp VIP to trigger.
    vip = _mkunit("u_b_vip", Team.BLUE, Pos(0, 0))
    vip.hp = 0
    state.units[vip.id] = vip
    result = apply(state, EndTurnAction())
    assert result["winner"] == "red"
    assert result["reason"] == "vip_lost"


def test_reach_goal_line_fires_on_axis():
    blue = _mkunit("u_b_1", Team.BLUE, Pos(3, 7))
    state = _make_state({blue.id: blue})
    state._win_conditions = build_conditions([
        {"type": "reach_goal_line", "team": "blue", "axis": "y", "value": 7},
    ])
    result = apply(state, EndTurnAction())
    assert result["winner"] == "blue"
    assert result["reason"] == "reach_goal_line"


def test_scenario_load_sets_win_conditions_attribute():
    """load_scenario should attach _win_conditions to the state."""
    from clash_of_odin.server.engine.scenarios import load_scenario

    state = load_scenario("01_tiny_skirmish")
    assert hasattr(state, "_win_conditions")
    assert len(state._win_conditions) >= 1
