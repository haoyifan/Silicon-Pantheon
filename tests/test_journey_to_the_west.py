"""Smoke tests for the Journey to the West scenario."""

from __future__ import annotations

from silicon_pantheon.server.engine.scenarios import load_scenario


def test_jttw_loads():
    state = load_scenario("journey_to_the_west")
    assert state.board.width == 22
    assert state.board.height == 14
    # Blue pilgrims + red monsters.
    blue_ids = {u.id for u in state.units.values() if u.owner.value == "blue"}
    red_ids = {u.id for u in state.units.values() if u.owner.value == "red"}
    assert "u_b_tang_monk_1" in blue_ids
    assert "u_b_sun_wukong_1" in blue_ids
    assert "u_r_demon_king_1" in red_ids


def test_jttw_has_win_conditions_including_protect_and_reach():
    state = load_scenario("journey_to_the_west")
    names = [type(r).__name__ for r in state._win_conditions]
    assert "ProtectUnit" in names
    assert "ReachTile" in names


def test_jttw_plugin_namespace_has_spawn_ambush():
    state = load_scenario("journey_to_the_west")
    assert "spawn_ambush" in state._plugin_namespace
    assert "spawn_ambush" in state._turn_start_hooks


def test_jttw_narrative_events_parsed():
    state = load_scenario("journey_to_the_west")
    assert state._narrative.title == "Journey to the West"
    # Expect at least the three turn-based events + two death events.
    assert len(state._narrative.events) >= 5
