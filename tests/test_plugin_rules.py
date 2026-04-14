"""Scenario plugin loader + PluginRule end-to-end.

Uses the synthetic `_test_plugin` scenario in games/.
"""

from __future__ import annotations

from clash_of_odin.server.engine.rules import EndTurnAction, apply
from clash_of_odin.server.engine.scenarios import load_scenario
from clash_of_odin.server.engine.state import GameStatus


def test_plugin_namespace_exposes_public_callables_only():
    state = load_scenario("_test_plugin")
    ns = state._plugin_namespace
    assert "always_blue_wins" in ns
    assert "some_helper" in ns
    assert "_private_helper" not in ns
    assert ns["some_helper"](4) == 8


def test_plugin_win_rule_fires_via_dsl():
    state = load_scenario("_test_plugin")
    result = apply(state, EndTurnAction())
    assert result["winner"] == "blue"
    assert result["reason"] == "test_plugin"
    assert state.status is GameStatus.GAME_OVER


def test_scenario_without_rules_py_has_empty_namespace():
    state = load_scenario("01_tiny_skirmish")
    assert state._plugin_namespace == {}
