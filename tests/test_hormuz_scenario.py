"""Smoke tests for the Strait of Hormuz scenario (modern redesign).

The scenario was rewritten as a speculative modern fiction
("Operation Epic Fury 2026"): US/Israel strike force vs Iranian
coastal defense. Blue VIP is Trump; red VIP is Khamenei.

Pins the core win-condition branches:
  1. scenario loads with expected unit counts
  2. blue VIP (Trump) killed → red wins via protect_unit
  3. game starts without a winner
"""

from __future__ import annotations

from silicon_pantheon.server.engine.scenarios import load_scenario


def _kill(state, uid: str) -> None:
    """Simulate a unit's death."""
    if uid in state.units:
        u = state.units[uid]
        u.hp = 0
        state.fallen_units[uid] = u
        del state.units[uid]
    state.dead_unit_ids.add(uid)


def test_scenario_loads_with_expected_shape():
    s = load_scenario("13_hormuz")
    blue = [u for u in s.units.values() if u.owner.value == "blue"]
    red = [u for u in s.units.values() if u.owner.value == "red"]
    assert len(blue) == 7
    assert len(red) == 7
    assert s.max_turns == 16


def test_trump_death_loses_for_blue():
    """protect_unit: if Trump dies, blue loses."""
    s = load_scenario("13_hormuz")
    assert "u_b_trump_1" in s.units
    _kill(s, "u_b_trump_1")
    from silicon_pantheon.server.engine.rules import EndTurnAction, apply
    result = apply(s, EndTurnAction())
    assert result.get("winner") == "red"


def test_game_starts_without_winner():
    s = load_scenario("13_hormuz")
    from silicon_pantheon.server.engine.rules import EndTurnAction, apply
    result = apply(s, EndTurnAction())
    assert result.get("winner") is None
