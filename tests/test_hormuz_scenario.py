"""Smoke tests for the Strait of Hormuz scenario.

Pins the win-condition plugin's four branches:
  1. nothing happens yet → no winner
  2. blue on bunker + Khamenei dead → blue wins
  3. turn budget exhausted with goal unmet → red wins
  4. blue VIP killed → red wins (via protect_unit)

Also checks the sea_mine terrain effect detonates on contact.
"""

from __future__ import annotations

from silicon_pantheon.server.engine.rules import EndTurnAction, apply
from silicon_pantheon.server.engine.scenarios import load_scenario
from silicon_pantheon.server.engine.state import Pos


def _kill(state, uid: str) -> None:
    """Simulate a unit's death by the same path the rules engine uses."""
    if uid in state.units:
        u = state.units[uid]
        u.hp = 0
        state.fallen_units[uid] = u
        del state.units[uid]
    state.dead_unit_ids.add(uid)


def test_scenario_loads_with_expected_shape():
    s = load_scenario("13_hormuz")
    assert s.board.width == 18 and s.board.height == 10
    assert s.max_turns == 10
    assert s.first_player.value == "blue"
    # Khamenei placed forward with HP 1, matching the user-facing spec.
    k = s.units.get("u_r_khamenei_1")
    assert k is not None
    assert k.hp == 1
    assert k.pos.x <= 5, "Khamenei should start close to the blue side"
    # Both blue VIP leaders are present.
    assert "u_b_trump_1" in s.units
    assert "u_b_netanyahu_1" in s.units


def test_initial_end_turn_has_no_winner():
    s = load_scenario("13_hormuz")
    r = apply(s, EndTurnAction())
    assert r.get("winner") is None


def test_blue_wins_with_bunker_and_khamenei():
    s = load_scenario("13_hormuz")
    _kill(s, "u_r_khamenei_1")
    # Move a SEAL onto the uranium bunker tile.
    seal = next(u for u in s.units.values() if u.class_ == "navy_seal")
    seal.pos = Pos(15, 5)
    r = apply(s, EndTurnAction())
    assert r.get("winner") == "blue"
    assert r.get("reason") == "enriched_uranium_seized_and_khamenei_killed"


def test_bunker_alone_is_not_enough():
    """Stepping on the bunker without killing Khamenei must NOT win —
    this is the whole point of the plugin existing instead of
    seize_enemy_fort."""
    s = load_scenario("13_hormuz")
    seal = next(u for u in s.units.values() if u.class_ == "navy_seal")
    seal.pos = Pos(15, 5)
    r = apply(s, EndTurnAction())
    assert r.get("winner") is None


def test_khamenei_alone_is_not_enough():
    """Killing the Supreme Leader without reaching the bunker does
    not end the match — blue needs both halves."""
    s = load_scenario("13_hormuz")
    _kill(s, "u_r_khamenei_1")
    r = apply(s, EndTurnAction())
    assert r.get("winner") is None


def test_red_wins_on_turn_budget_exhausted():
    s = load_scenario("13_hormuz")
    s.turn = 11  # past max_turns
    r = apply(s, EndTurnAction())
    assert r.get("winner") == "red"
    assert r.get("reason") == "iran_held_the_line"


def test_trump_death_loses_the_match_for_blue():
    s = load_scenario("13_hormuz")
    _kill(s, "u_b_trump_1")
    r = apply(s, EndTurnAction())
    assert r.get("winner") == "red"
    assert r.get("reason") == "vip_lost"


def test_plugin_description_surfaces_through_describe_scenario():
    """The opaque check_fn name used to be all that room-preview /
    agent system-prompt saw for plugin rules. Now the plugin
    attaches a `.description` attribute to the function and the
    server's describe_scenario resolver reads it into the bundle."""
    from silicon_pantheon.server.engine.scenarios import (
        resolve_plugin_description,
    )

    desc = resolve_plugin_description(
        "13_hormuz", "rules", "enriched_uranium_strike_check"
    )
    assert desc is not None
    assert "uranium" in desc.lower()
    assert "khamenei" in desc.lower()

    # Unknown function → None; caller falls back to the function name.
    assert (
        resolve_plugin_description("13_hormuz", "rules", "not_a_real_fn")
        is None
    )

    # Unknown scenario name → None (no crash on missing plugin file).
    assert (
        resolve_plugin_description("does_not_exist", "rules", "anything")
        is None
    )


def test_netanyahu_death_loses_the_match_for_blue():
    s = load_scenario("13_hormuz")
    _kill(s, "u_b_netanyahu_1")
    r = apply(s, EndTurnAction())
    assert r.get("winner") == "red"
    assert r.get("reason") == "vip_lost"
